# OpenShift Rolling Reboot Ansible Module

This directory contains a custom Ansible module designed to execute a parallel rolling reboot / maintenance pipeline for OpenShift nodes.

## Features

1. **Cordon & Drain**: Automatically marks nodes as unschedulable and drains workloads gracefully before reboot.
2. **Reboot Strategies**:
   - `oc_debug` (Default): Reboots nodes using `oc debug node/<name>` - ideal for environments where SSH access is restricted but cluster-admin access is available.
   - `ssh`: Reboots nodes via standard SSH using `systemctl reboot`.
   - `command`: Executes a custom reboot command template where `{node}` is replaced by the node name.
3. **Queue-based Parallelism**: Control the maximum concurrency with the `parallel` parameter. Workers pull nodes from the queue, so as soon as any node completes its reboot and transitions to `Ready`, the next node in line begins its maintenance.
4. **Reliable Reboot Verification**: Reads the unique Linux boot ID (`/proc/sys/kernel/random/boot_id`) of the node before rebooting, and polls until a new boot ID is detected and the Kubernetes node status is `Ready`.
5. **Uncordon**: Safely returns the node to service.
6. **Authentication Integration**: Supports passing explicit `kubeconfig`, `api_host` (server), and `api_key` (token) settings to target remote/isolated OpenShift environments directly.

## Requirements

- `oc` command line tool installed and authenticated to your OpenShift cluster on the control node.
- Ansible installed on the control node.

## Usage

Place the `library/` folder containing the module in the same directory as your playbook.

### Module Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `nodes` | list | (Required) | List of OpenShift node names. |
| `parallel` | int | `1` | Max number of nodes to reboot concurrently. |
| `drain_timeout` | int | `600` | Evacuation timeout per node in seconds. |
| `reboot_timeout` | int | `600` | Recovery timeout per node in seconds. |
| `reboot_method` | str | `oc_debug` | Reboot mechanism (`oc_debug`, `ssh`, or `command`). |
| `ssh_user` | str | `None` | SSH username if using `ssh` reboot method. |
| `ssh_key` | str | `None` | SSH private key file path if using `ssh` reboot method. |
| `custom_reboot_command` | str | `None` | Custom command template (e.g. `ipmitool -H {node}-ilo power reset`). |
| `uncordon` | bool | `true` | Restores scheduling on completion. |
| `ignore_daemonsets` | bool | `true` | Ignore daemonsets during drain. |
| `delete_emptydir_data` | bool | `true` | Allow deleting emptyDir data on drain. |
| `force_drain` | bool | `true` | Force drain even if there are unmanaged pods. |
| `kubeconfig` | str | `None` | Path to the kubeconfig file to use for authentication. |
| `api_host` | str | `None` | OpenShift API server URL (equivalent to `oc --server`). |
| `api_key` | str | `None` | Token to authenticate with OpenShift API (equivalent to `oc --token`). |

### Example Playbook

See [playbook.yml](file:///Users/sreenichenna/Downloads/projects/ansible_rolling_reboot/playbook.yml):

```yaml
---
- name: OpenShift Rolling Reboot Playbook
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Perform parallel rolling reboot of cluster nodes
      openshift_rolling_reboot:
        nodes:
          - worker-0.example.com
          - worker-1.example.com
          - worker-2.example.com
          - worker-3.example.com
        parallel: 2
        reboot_method: oc_debug
        drain_timeout: 300
        reboot_timeout: 450
        uncordon: true
        kubeconfig: /path/to/kubeconfig
        api_host: https://api.openshift.example.com:6443
        api_key: sha256~xxxxxx
      register: reboot_result

    - name: Show reboot results
      ansible.builtin.debug:
        var: reboot_result
```
