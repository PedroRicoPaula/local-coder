import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import detect_hardware as dh  # noqa: E402  (path insert must come first)


class TestTier(unittest.TestCase):
    def test_gpu_present_wins_regardless_of_cpu_ram(self):
        self.assertEqual(dh._tier(physical_cores=1, ram_gb=2, has_gpu=True), "gpu")

    def test_strong_cpu_and_enough_ram_is_cpu_strong(self):
        self.assertEqual(dh._tier(physical_cores=4, ram_gb=16, has_gpu=False), "cpu-strong")

    def test_below_either_threshold_is_cpu_weak(self):
        self.assertEqual(dh._tier(physical_cores=3, ram_gb=32, has_gpu=False), "cpu-weak")
        self.assertEqual(dh._tier(physical_cores=8, ram_gb=8, has_gpu=False), "cpu-weak")

    def test_this_reference_machine_is_cpu_weak(self):
        """Confirmed live on the actual reference machine (2 physical
        cores, ~19GB RAM, no usable GPU) -- pinning this here so a future
        threshold change has to consciously decide whether it still wants
        to call this machine "weak"."""
        self.assertEqual(dh._tier(physical_cores=2, ram_gb=19.4, has_gpu=False), "cpu-weak")


class TestPhysicalCores(unittest.TestCase):
    def test_counts_unique_physical_core_id_pairs(self):
        cpuinfo = "\n".join([
            "processor\t: 0", "physical id\t: 0", "core id\t: 0",
            "processor\t: 1", "physical id\t: 0", "core id\t: 1",
            "processor\t: 2", "physical id\t: 0", "core id\t: 0",  # HT sibling of core 0
            "processor\t: 3", "physical id\t: 0", "core id\t: 1",  # HT sibling of core 1
        ])
        with mock.patch.object(Path, "read_text", return_value=cpuinfo):
            self.assertEqual(dh._physical_cores(), 2)

    def test_missing_physical_id_field_still_counts_unique_core_ids(self):
        cpuinfo = "\n".join(["processor\t: 0", "core id\t: 0", "processor\t: 1", "core id\t: 1"])
        with mock.patch.object(Path, "read_text", return_value=cpuinfo):
            self.assertEqual(dh._physical_cores(), 2)

    def test_unreadable_proc_cpuinfo_falls_back_to_cpu_count(self):
        with mock.patch.object(Path, "read_text", side_effect=OSError), mock.patch(
            "os.cpu_count", return_value=8
        ):
            self.assertEqual(dh._physical_cores(), 8)


class TestGpu(unittest.TestCase):
    def test_no_nvidia_smi_means_no_gpu(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(dh._gpu(), (False, None))

    def test_nvidia_smi_present_but_no_driver_reports_no_gpu(self):
        """The reference machine's own situation: an old, driverless GPU
        with no nvidia-smi binary at all -- covered by the no-nvidia-smi
        case above. This covers the adjacent case where the binary exists
        but fails to actually talk to a card (e.g. a stale/partial driver
        install)."""
        failed = mock.Mock(returncode=1, stdout="")
        with mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"), mock.patch(
            "subprocess.run", return_value=failed
        ):
            self.assertEqual(dh._gpu(), (False, None))


if __name__ == "__main__":
    unittest.main()
