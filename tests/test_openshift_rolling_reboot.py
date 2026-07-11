import sys
import unittest
from unittest.mock import patch, MagicMock

# Dynamically mock ansible imports to allow loading the module without ansible package
mock_ansible = MagicMock()
sys.modules['ansible'] = mock_ansible
sys.modules['ansible.module_utils'] = mock_ansible
sys.modules['ansible.module_utils.basic'] = mock_ansible

# Add library path to sys.path so we can import the module
sys.path.append('plugins/modules')
import openshift_rolling_reboot


class TestOpenShiftRollingReboot(unittest.TestCase):

    @patch('openshift_rolling_reboot.subprocess.Popen')
    def test_run_cmd_success(self, mock_popen):
        # Setup mock process
        mock_process = MagicMock()
        mock_process.communicate.return_value = (b"output message", b"error message")
        mock_process.returncode = 0
        mock_popen.return_value = mock_process

        rc, stdout, stderr = openshift_rolling_reboot.run_cmd("echo 'hello'", timeout=10)

        self.assertEqual(rc, 0)
        self.assertEqual(stdout, "output message")
        self.assertEqual(stderr, "error message")
        mock_popen.assert_called_once_with("echo 'hello'", shell=True, stdout=-1, stderr=-1)

    def test_get_oc_prefix_with_auth(self):
        params = {
            'kubeconfig': '/path/to/config',
            'api_host': 'https://api.openshift.com:6443',
            'api_key': 'sha256~token123'
        }
        prefix = openshift_rolling_reboot.get_oc_prefix(params)
        self.assertEqual(prefix, "oc --kubeconfig=/path/to/config --server=https://api.openshift.com:6443 --token=sha256~token123")

    @patch('openshift_rolling_reboot.run_cmd')
    def test_get_boot_id_oc_debug_with_auth(self, mock_run_cmd):
        mock_run_cmd.return_value = (0, "uuid-1234", "")
        params = {
            'reboot_method': 'oc_debug',
            'kubeconfig': '/path/to/config',
            'api_host': 'https://api.openshift.com:6443',
            'api_key': 'sha256~token123'
        }
        
        boot_id = openshift_rolling_reboot.get_boot_id("node-1", params)
        
        self.assertEqual(boot_id, "uuid-1234")
        mock_run_cmd.assert_called()
        executed_cmd = mock_run_cmd.call_args[0][0]
        self.assertIn("oc --kubeconfig=/path/to/config --server=https://api.openshift.com:6443 --token=sha256~token123", executed_cmd)

    @patch('openshift_rolling_reboot.run_cmd')
    def test_get_boot_id_ssh(self, mock_run_cmd):
        mock_run_cmd.return_value = (0, "uuid-5678", "")
        params = {
            'reboot_method': 'ssh',
            'ssh_user': 'core',
            'ssh_key': '/path/to/key'
        }
        
        boot_id = openshift_rolling_reboot.get_boot_id("node-1", params)
        
        self.assertEqual(boot_id, "uuid-5678")
        mock_run_cmd.assert_called()
        self.assertIn("ssh -o ConnectTimeout=10", mock_run_cmd.call_args[0][0])
        self.assertIn("core@node-1", mock_run_cmd.call_args[0][0])

    @patch('openshift_rolling_reboot.run_cmd')
    def test_trigger_node_reboot_oc_debug(self, mock_run_cmd):
        mock_run_cmd.return_value = (0, "reboot triggered", "")
        params = {
            'reboot_method': 'oc_debug'
        }
        
        rc, out, err = openshift_rolling_reboot.trigger_node_reboot("node-1", params)
        
        self.assertEqual(rc, 0)
        self.assertIn("systemctl reboot", mock_run_cmd.call_args[0][0])

    @patch('openshift_rolling_reboot.run_cmd')
    def test_is_node_ready_true(self, mock_run_cmd):
        mock_run_cmd.return_value = (0, "True", "")
        params = {
            'kubeconfig': '/path/to/config'
        }
        
        ready = openshift_rolling_reboot.is_node_ready("node-1", params)
        
        self.assertTrue(ready)
        self.assertIn("--kubeconfig=/path/to/config", mock_run_cmd.call_args[0][0])

    @patch('openshift_rolling_reboot.is_node_ready')
    @patch('openshift_rolling_reboot.get_boot_id')
    @patch('openshift_rolling_reboot.trigger_node_reboot')
    @patch('openshift_rolling_reboot.run_cmd')
    def test_process_node_success_with_boot_id(self, mock_run_cmd, mock_reboot, mock_get_boot_id, mock_is_ready):
        mock_run_cmd.return_value = (0, "success", "")
        mock_reboot.return_value = (0, "rebooting", "")
        
        # get_boot_id call order:
        # 1. Before reboot: "uuid-1"
        # 2. In loop iteration 1: "uuid-2"
        mock_get_boot_id.side_effect = ["uuid-1", "uuid-2"]
        mock_is_ready.return_value = True

        params = {
            'reboot_method': 'oc_debug',
            'ssh_user': None,
            'ssh_key': None,
            'custom_reboot_command': None,
            'drain_timeout': 60,
            'reboot_timeout': 30,
            'ignore_daemonsets': True,
            'delete_emptydir_data': True,
            'force_drain': True,
            'uncordon': True,
            'kubeconfig': None,
            'api_host': None,
            'api_key': None
        }
        results = {}
        import threading
        lock = threading.Lock()

        with patch('openshift_rolling_reboot.time.sleep') as mock_sleep:
            openshift_rolling_reboot.process_node("node-1", params, results, lock)

        self.assertIn("node-1", results)
        self.assertEqual(results["node-1"]["status"], "success")
        self.assertEqual(results["node-1"]["reboot"], "success")

    @patch('openshift_rolling_reboot.is_node_ready')
    @patch('openshift_rolling_reboot.get_boot_id')
    @patch('openshift_rolling_reboot.trigger_node_reboot')
    @patch('openshift_rolling_reboot.run_cmd')
    def test_process_node_success_fallback_no_boot_id(self, mock_run_cmd, mock_reboot, mock_get_boot_id, mock_is_ready):
        mock_run_cmd.return_value = (0, "success", "")
        mock_reboot.return_value = (0, "rebooting", "")
        
        # Initial boot_id fails (returns None)
        # Inside loop: current_boot_id returns None (node offline), then None
        mock_get_boot_id.side_effect = [None, None, None]
        
        # is_node_ready calls in loop:
        # Iteration 1: False (node offline)
        # Iteration 2: True (node online again)
        mock_is_ready.side_effect = [False, True]

        params = {
            'reboot_method': 'oc_debug',
            'ssh_user': None,
            'ssh_key': None,
            'custom_reboot_command': None,
            'drain_timeout': 60,
            'reboot_timeout': 30,
            'ignore_daemonsets': True,
            'delete_emptydir_data': True,
            'force_drain': True,
            'uncordon': True,
            'kubeconfig': None,
            'api_host': None,
            'api_key': None
        }
        results = {}
        import threading
        lock = threading.Lock()

        with patch('openshift_rolling_reboot.time.sleep') as mock_sleep:
            openshift_rolling_reboot.process_node("node-1", params, results, lock)

        self.assertIn("node-1", results)
        self.assertEqual(results["node-1"]["status"], "success")
        self.assertEqual(results["node-1"]["reboot"], "success")

    @patch('openshift_rolling_reboot.is_node_ready')
    @patch('openshift_rolling_reboot.get_boot_id')
    @patch('openshift_rolling_reboot.trigger_node_reboot')
    @patch('openshift_rolling_reboot.run_cmd')
    def test_process_node_success_zero_timeouts(self, mock_run_cmd, mock_reboot, mock_get_boot_id, mock_is_ready):
        mock_run_cmd.return_value = (0, "success", "")
        mock_reboot.return_value = (0, "rebooting", "")
        mock_get_boot_id.side_effect = ["uuid-1", "uuid-2"]
        mock_is_ready.return_value = True

        params = {
            'reboot_method': 'oc_debug',
            'ssh_user': None,
            'ssh_key': None,
            'custom_reboot_command': None,
            'drain_timeout': 0, # zero timeout (infinite)
            'reboot_timeout': 0, # zero timeout (infinite)
            'ignore_daemonsets': True,
            'delete_emptydir_data': True,
            'force_drain': True,
            'uncordon': True,
            'kubeconfig': None,
            'api_host': None,
            'api_key': None
        }
        results = {}
        import threading
        lock = threading.Lock()

        with patch('openshift_rolling_reboot.time.sleep') as mock_sleep:
            openshift_rolling_reboot.process_node("node-1", params, results, lock)

        self.assertIn("node-1", results)
        self.assertEqual(results["node-1"]["status"], "success")
        self.assertEqual(results["node-1"]["reboot"], "success")
        
        # Verify that "--timeout" was NOT in the command because timeout is 0
        executed_drain_cmd = mock_run_cmd.call_args_list[1][0][0]
        self.assertNotIn("--timeout", executed_drain_cmd)


if __name__ == '__main__':
    unittest.main()
