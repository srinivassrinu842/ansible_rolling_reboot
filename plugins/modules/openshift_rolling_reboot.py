#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = r'''
---
module: openshift_rolling_reboot
short_description: Perform parallel rolling reboot and maintenance of OpenShift nodes
description:
  - Cordon, drain, reboot, wait for recovery, and uncordon OpenShift nodes.
  - Supports a true queue-based parallel execution limit (concurrency queue) where a new node starts maintenance as soon as any running node finishes rebooting.
options:
  nodes:
    description:
      - List of OpenShift node names to perform maintenance on.
    type: list
    elements: str
    required: true
  parallel:
    description:
      - Maximum number of nodes to reboot/maintain concurrently.
    type: int
    default: 1
  drain_timeout:
    description:
      - Timeout in seconds for the cordon and drain operation on a single node. Set to 0 for no timeout.
    type: int
    default: 0
  reboot_timeout:
    description:
      - Timeout in seconds to wait for a node to reboot and become 'Ready'. Set to 0 for no timeout.
    type: int
    default: 0
  reboot_method:
    description:
      - Method used to trigger the reboot on the node.
      - C(oc_debug) runs a debug pod on the node and triggers systemctl reboot.
      - C(ssh) runs ssh to trigger systemctl reboot.
      - C(command) runs a custom local command.
    type: str
    choices: [ oc_debug, ssh, command ]
    default: oc_debug
  ssh_user:
    description:
      - SSH user to use if C(reboot_method) is C(ssh).
    type: str
  ssh_key:
    description:
      - Path to the SSH private key to use if C(reboot_method) is C(ssh).
    type: str
  custom_reboot_command:
    description:
      - Custom command template to run for rebooting a node if C(reboot_method) is C(command).
      - The string C({node}) will be replaced by the node name.
    type: str
  uncordon:
    description:
      - Whether to uncordon the node after successful reboot.
    type: bool
    default: true
  ignore_daemonsets:
    description:
      - Ignore DaemonSet-managed pods during drain.
    type: bool
    default: true
  delete_emptydir_data:
    description:
      - Continue even if there are pods using emptyDir volumes.
    type: bool
    default: true
  force_drain:
    description:
      - Continue even if there are pods not managed by a controller.
    type: bool
    default: true
  kubeconfig:
    description:
      - Path to the kubeconfig file to use for authentication.
    type: str
  api_host:
    description:
      - The OpenShift API server URL (equivalent to --server).
    type: str
  api_key:
    description:
      - Token to authenticate with the OpenShift API server (equivalent to --token).
    type: str
    no_log: true
author:
  - Antigravity
'''

EXAMPLES = r'''
- name: Perform rolling reboot of worker nodes (parallel 2)
  openshift.maintenance.openshift_rolling_reboot:
    nodes:
      - worker-0.example.com
      - worker-1.example.com
    parallel: 2
    reboot_method: oc_debug
    kubeconfig: /path/to/kubeconfig
    api_host: https://api.openshift.example.com:6443
    api_key: sha256~xxxxxx
'''

import queue
import subprocess
import threading
import time
from ansible.module_utils.basic import AnsibleModule


def run_cmd(cmd, timeout=None):
    """Helper to run a shell command and return returncode, stdout, stderr"""
    try:
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = p.communicate(timeout=timeout)
        return p.returncode, stdout.decode('utf-8', errors='ignore').strip(), stderr.decode('utf-8', errors='ignore').strip()
    except subprocess.TimeoutExpired:
        p.kill()
        stdout, stderr = p.communicate()
        return -1, stdout.decode('utf-8', errors='ignore').strip(), stderr.decode('utf-8', errors='ignore').strip() + "\nCommand timed out"
    except Exception as e:
        return -1, "", str(e)


def get_oc_prefix(params):
    """Generates the oc command prefix with auth parameters if supplied"""
    prefix = "oc"
    if params.get('kubeconfig'):
        prefix += " --kubeconfig={}".format(params['kubeconfig'])
    if params.get('api_host'):
        prefix += " --server={}".format(params['api_host'])
    if params.get('api_key'):
        prefix += " --token={}".format(params['api_key'])
    return prefix


def get_boot_id(node, params):
    """Fetch the boot_id of a node using the chosen reboot method"""
    reboot_method = params['reboot_method']
    if reboot_method == 'oc_debug':
        oc = get_oc_prefix(params)
        cmd = "{} debug node/{} --as-user=system:admin --one-container=true --quiet=true -- chroot /host cat /proc/sys/kernel/random/boot_id".format(oc, node)
        rc, out, err = run_cmd(cmd, timeout=30)
        if rc == 0 and out:
            return out
        cmd = "{} debug node/{} --one-container=true --quiet=true -- chroot /host cat /proc/sys/kernel/random/boot_id".format(oc, node)
        rc, out, err = run_cmd(cmd, timeout=30)
        if rc == 0 and out:
            return out
    elif reboot_method == 'ssh':
        ssh_opts = "-o ConnectTimeout=10 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        if params.get('ssh_key'):
            ssh_opts += " -i {}".format(params['ssh_key'])
        user_prefix = "{}@".format(params['ssh_user']) if params.get('ssh_user') else ""
        cmd = "ssh {} {}{} 'cat /proc/sys/kernel/random/boot_id'".format(ssh_opts, user_prefix, node)
        rc, out, err = run_cmd(cmd, timeout=20)
        if rc == 0 and out:
            return out
    return None


def trigger_node_reboot(node, params):
    """Triggers the reboot command on the node"""
    reboot_method = params['reboot_method']
    if reboot_method == 'oc_debug':
        oc = get_oc_prefix(params)
        cmd = "{} debug node/{} --as-user=system:admin --one-container=true --quiet=true -- chroot /host systemctl reboot".format(oc, node)
        rc, out, err = run_cmd(cmd, timeout=40)
        if rc != 0:
            cmd = "{} debug node/{} --one-container=true --quiet=true -- chroot /host systemctl reboot".format(oc, node)
            rc, out, err = run_cmd(cmd, timeout=40)
        return rc, out, err
    elif reboot_method == 'ssh':
        ssh_opts = "-o ConnectTimeout=10 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
        if params.get('ssh_key'):
            ssh_opts += " -i {}".format(params['ssh_key'])
        user_prefix = "{}@".format(params['ssh_user']) if params.get('ssh_user') else ""
        cmd = "ssh {} {}{} 'sudo systemctl reboot'".format(ssh_opts, user_prefix, node)
        return run_cmd(cmd, timeout=25)
    elif reboot_method == 'command':
        if not params.get('custom_reboot_command'):
            return -1, "", "custom_reboot_command is required when reboot_method=command"
        cmd = params['custom_reboot_command'].format(node=node)
        return run_cmd(cmd, timeout=60)
    return -1, "", "Unknown reboot method"


def is_node_ready(node, params):
    """Check if node is Ready according to Kubernetes API"""
    oc = get_oc_prefix(params)
    cmd = "{} get node {} -o jsonpath='{{.status.conditions[?(@.type==\"Ready\")].status}}'".format(oc, node)
    rc, out, err = run_cmd(cmd, timeout=20)
    if rc == 0 and out == "True":
        return True
    return False


def process_node(node, params, results, lock):
    """Worker function to process a single node's maintenance cycle"""
    node_result = {
        "status": "failed",
        "cordon": "pending",
        "drain": "pending",
        "reboot": "pending",
        "uncordon": "pending",
        "msg": ""
    }

    oc = get_oc_prefix(params)

    # 1. Cordon the node
    cordon_cmd = "{} adm cordon {}".format(oc, node)
    rc, out, err = run_cmd(cordon_cmd, timeout=30)
    if rc != 0:
        node_result["cordon"] = "failed"
        node_result["msg"] = "Cordon failed: {}".format(err or out)
        with lock:
            results[node] = node_result
        return

    node_result["cordon"] = "success"

    # 2. Drain the node
    drain_cmd = "{} adm drain {}".format(oc, node)
    if params['ignore_daemonsets']:
        drain_cmd += " --ignore-daemonsets"
    if params['delete_emptydir_data']:
        drain_cmd += " --delete-emptydir-data"
    if params['force_drain']:
        drain_cmd += " --force"
    
    drain_timeout_val = None
    if params['drain_timeout'] > 0:
        drain_cmd += " --timeout={}s".format(params['drain_timeout'])
        drain_timeout_val = params['drain_timeout'] + 10

    rc, out, err = run_cmd(drain_cmd, timeout=drain_timeout_val)
    if rc != 0:
        node_result["drain"] = "failed"
        node_result["msg"] = "Drain failed: {}".format(err or out)
        run_cmd("{} adm uncordon {}".format(oc, node), timeout=30)
        with lock:
            results[node] = node_result
        return

    node_result["drain"] = "success"

    # 3. Get initial boot ID
    initial_boot_id = get_boot_id(node, params)

    # 4. Trigger reboot
    rc, out, err = trigger_node_reboot(node, params)
    node_result["reboot"] = "initiated"

    # 5. Wait for node to reboot and become Ready
    start_time = time.time()
    rebooted_successfully = False
    node_went_down = False

    # Sleep to allow the reboot to initiate and shut down services
    time.sleep(20)

    while True:
        # Check timeout if reboot_timeout is set
        if params['reboot_timeout'] > 0 and (time.time() - start_time >= params['reboot_timeout']):
            break

        current_boot_id = get_boot_id(node, params)
        ready = is_node_ready(node, params)

        # Track if the node went offline or unreachable
        if not ready or current_boot_id is None:
            node_went_down = True

        if initial_boot_id:
            # Case 1: We successfully retrieved the initial boot ID.
            # We must verify:
            # - Node is reporting Ready=True in Kubernetes.
            # - We fetched a valid new boot ID.
            # - The new boot ID is different from the initial boot ID.
            if ready and current_boot_id and (current_boot_id != initial_boot_id):
                rebooted_successfully = True
                node_result["reboot"] = "success"
                break
        else:
            # Case 2: Fallback if we failed to retrieve initial boot ID.
            # We must verify:
            # - We observed the node transitioning to offline/unreachable.
            # - Node has transitioned back to Ready=True in Kubernetes.
            if node_went_down and ready:
                rebooted_successfully = True
                node_result["reboot"] = "success"
                break

        time.sleep(10)

    if not rebooted_successfully:
        node_result["reboot"] = "failed"
        node_result["msg"] = "Timed out waiting for reboot or Node failed to become Ready."
        with lock:
            results[node] = node_result
        return

    # 6. Uncordon the node
    if params['uncordon']:
        uncordon_cmd = "{} adm uncordon {}".format(oc, node)
        rc, out, err = run_cmd(uncordon_cmd, timeout=30)
        if rc != 0:
            node_result["uncordon"] = "failed"
            node_result["msg"] = "Uncordon failed: {}".format(err or out)
            with lock:
                results[node] = node_result
            return
        node_result["uncordon"] = "success"
    else:
        node_result["uncordon"] = "skipped"

    node_result["status"] = "success"
    node_result["msg"] = "Node successfully rebooted and uncordoned"
    with lock:
        results[node] = node_result


def main():
    module = AnsibleModule(
        argument_spec=dict(
            nodes=dict(type='list', elements='str', required=True),
            parallel=dict(type='int', default=1),
            drain_timeout=dict(type='int', default=0),
            reboot_timeout=dict(type='int', default=0),
            reboot_method=dict(type='str', choices=['oc_debug', 'ssh', 'command'], default='oc_debug'),
            ssh_user=dict(type='str', required=False),
            ssh_key=dict(type='str', required=False),
            custom_reboot_command=dict(type='str', required=False),
            uncordon=dict(type='bool', default=True),
            ignore_daemonsets=dict(type='bool', default=True),
            delete_emptydir_data=dict(type='bool', default=True),
            force_drain=dict(type='bool', default=True),
            kubeconfig=dict(type='str', required=False),
            api_host=dict(type='str', required=False),
            api_key=dict(type='str', required=False, no_log=True),
        ),
        supports_check_mode=True
    )

    nodes = module.params['nodes']
    parallel = module.params['parallel']

    if module.check_mode:
        module.exit_json(changed=True, msg="Check mode: Would perform rolling reboot on {} nodes with concurrency {}.".format(len(nodes), parallel))

    if parallel < 1:
        module.fail_json(msg="parallel parameter must be 1 or greater")

    node_queue = queue.Queue()
    for n in nodes:
        node_queue.put(n)

    results = {}
    results_lock = threading.Lock()

    def worker():
        while not node_queue.empty():
            try:
                node = node_queue.get_nowait()
            except queue.Empty:
                break
            
            try:
                process_node(node, module.params, results, results_lock)
            except Exception as e:
                with results_lock:
                    results[node] = {
                        "status": "failed",
                        "msg": "Unexpected exception in worker thread: {}".format(str(e))
                    }
            finally:
                node_queue.task_done()

    threads = []
    num_workers = min(parallel, len(nodes))
    for _ in range(num_workers):
        t = threading.Thread(target=worker)
        t.daemon = True
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    failed = False
    for node, result in results.items():
        if result.get("status") != "success":
            failed = True
            break

    if failed:
        module.fail_json(msg="One or more nodes failed during rolling reboot.", results=results)
    else:
        module.exit_json(changed=True, msg="All nodes successfully processed.", results=results)


if __name__ == '__main__':
    main()
