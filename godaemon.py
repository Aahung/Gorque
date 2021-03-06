#! /usr/bin/env python2.7

import subprocess
import os
import time
import goconfig
from godb import DB
from multiprocessing import Process
from time import sleep
from golog import golog
from torque import Torque


def background_run_job(job, node):
    config = goconfig.Config()
    daemon = Daemon(config)
    daemon.run_job(job, node)


class Daemon():

    config = None
    db = None

    def __init__(self, config):
        self.config = config
        self.db = DB(goconfig.DB_FILE)

    def get_qualified_jobs(self):
        # scan and get qualified jobs
        waiting_jobs = self.db.fetch_waiting()
        running_jobs = self.db.fetch_running()
        # calculate running job number for each user
        count = {}
        for job in running_jobs:
            user = job.get('user')
            if user not in count.keys():
                count[user] = 0
            count[user] = count[user] + 1
        # get qualified jobs
        qualified_jobs = []
        for job in waiting_jobs:
            user = job.get('user')
            if user not in count.keys():
                count[user] = 0
            if count[user] < self.config.get_user_job_limit(user):
                count[user] = count[user] + 1
                qualified_jobs.append(job)
        # sort by cpu desc then priority desc
        qualified_jobs.sort(key=lambda k: k.get('cpus'), reverse=True)
        qualified_jobs.sort(key=lambda k: k.get('priority'), reverse=True)
        return qualified_jobs

    def get_free_nodes(self):
        hosts_str = str.join(' ', self.config.hosts)
        command = ('''for i in %s;'''
                   '''do ssh ${i} 'mem=$(nvidia-smi | grep 4799 | cut -d"/" '''
                   '''-f3 | cut -d"|" -f2 | sed -e "s/^[ \t]*//"); '''
                   '''if [[ "$mem" == 11MiB* ]]; then echo $(hostname) | '''
                   '''cut -d"." -f1; fi'; done;''') % (hosts_str,)
        free_nodes = subprocess.check_output(command, shell=True)
        free_nodes = [x for x in free_nodes.split('\n') if x != '']
        return free_nodes

    def kill_torque_job(self, torque_pid):
        os.system('/opt/torque/bin/qdel %d' % (torque_pid,))

    def submit_torque_occupy_job(self, job):
        job_template = '''#PBS -S /bin/bash
#PBS -N gorque_shadow_%d
#PBS -l nodes=%s:ppn=%s
#PBS -q default

sleep 50000000'''
        job_script_file_path = '/tmp/gorque_torque_%s.sh' % (str(job.rowid),)
        job_script_file = open(job_script_file_path, 'w')
        job_script_file.write(job_template % (job.rowid, job.get('node'),
                                              str(job.get('cpus'))))
        job_script_file.close()
        try:
            command = ('''/sbin/runuser -l %s -c '/opt/torque/bin/qsub'''
                       ''' %s' ''') % (job.get('user'),
                                       job_script_file_path)
            process = subprocess.Popen(command, shell=True,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE)
            out, err = process.communicate()
            if len(err) != 0:
                golog('<%d> shadow job error: %s' % (job.rowid, err))
            torque_pid = out.split('.')[0]
            return int(torque_pid)
        except Exception, e:
            golog('<%d> shadow job error: %s' % (job.rowid, e))
            exit(2)

    def run_job(self, job, node):
        job.set('node', node)
        golog('<%d> submitting shadow job (torque)' % (job.rowid,))
        torque_pid = self.submit_torque_occupy_job(job)
        template = '''/sbin/runuser -l {0} -c 'ssh {1} "/bin/bash {2}"' '''
        tmp_script_path = '%s%s_%s.sh' % (self.config.job_script_dir,
                                          job.get('user'), str(job.rowid))
        golog('<%d> script content: \n%s' % (job.rowid, job.get('script')))
        golog('<%d> generating tmp script file' % (job.rowid,))
        f = open(tmp_script_path, 'w')
        f.write(job.get('script'))
        f.close()
        golog('<%d> executing' % (job.rowid,))
        command = template.format(job.get('user'), node, tmp_script_path)
        # run
        process = subprocess.Popen(command, shell=True,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        job.set('mode', 'R')
        job.set('start_time', int(time.time()))
        job.set('pid', process.pid)
        job.set('torque_pid', torque_pid)
        self.db.update(job)
        golog('<%d> finished' % (job.rowid,))
        # output logs
        out, err = process.communicate()
        f = open('%s%d.out' % (self.config.job_log_dir, job.rowid,), 'w')
        f.write(out)
        f.close()
        f = open('%s%d.err' % (self.config.job_log_dir, job.rowid,), 'w')
        f.write(err)
        f.close()
        # save the output or error
        # remove the sh file
        golog('<%d> removing tmp script file' % (job.rowid,))
        os.remove(tmp_script_path)
        golog('<%d> killing shadow job (torque)' % (job.rowid,))
        if job.get('torque_pid'):
            self.kill_torque_job(job.get('torque_pid'))
        # finish the job
        job.set('mode', 'F')
        job.set('end_time', int(time.time()))
        self.db.update(job)

    def scan(self):
        while True:
            qualified_jobs = self.get_qualified_jobs()
            if len(qualified_jobs) > 0:
                # scan for free GPUs
                free_nodes = self.get_free_nodes()
                free_cpus = Torque.free_cpus_in_nodes(free_nodes)
                free_nodes = sorted(free_cpus, reverse=True)
                if len(free_nodes) > 0:
                    # execute the job
                    i_node = 0
                    i_job = 0
                    while i_node < len(free_nodes) and i_job < len(qualified_jobs):
                        cpus_required = qualified_jobs[i_job].get('cpus')
                        cpus_available = free_cpus[free_nodes[i_node]]
                        if cpus_available >= cpus_required:
                            # run the job
                            p = Process(target=background_run_job,
                                        args=(qualified_jobs[i_job],
                                              free_nodes[i_node]))
                            p.start()
                            i_job = i_job + 1
                            i_node = i_node + 1
                        else:
                            i_node = i_node + 1
                else:
                    golog('no free nodes available')
            else:
                golog('no qualified jobs found')
            sleep(10)


def main():
    config = goconfig.Config()
    daemon = Daemon(config)
    print 'Gorque daemon on'
    daemon.scan()

if __name__ == "__main__":
    main()
