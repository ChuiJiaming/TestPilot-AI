import unittest
from uuid import UUID

from apps.api.app.environments.snapshots import EnvironmentSnapshot, build_environment_snapshot


class EnvironmentSnapshotTests(unittest.TestCase):
    def test_snapshot_hash_ignores_network_policy_key_order(self):
        first = build_environment_snapshot(
            environment_id=UUID("11111111-1111-4111-8111-111111111111"),
            project_id=UUID("22222222-2222-4222-8222-222222222222"),
            base_url="https://staging.example.test",
            network_policy={"hosts": ["staging.example.test"], "allow_redirects": False},
            secret_ref_version_id=UUID("33333333-3333-4333-8333-333333333333"),
        )
        second = build_environment_snapshot(
            environment_id=UUID("11111111-1111-4111-8111-111111111111"),
            project_id=UUID("22222222-2222-4222-8222-222222222222"),
            base_url="https://staging.example.test",
            network_policy={"allow_redirects": False, "hosts": ["staging.example.test"]},
            secret_ref_version_id=UUID("33333333-3333-4333-8333-333333333333"),
        )

        self.assertEqual(first.snapshot_hash, second.snapshot_hash)
        self.assertIsInstance(first, EnvironmentSnapshot)

    def test_secret_reference_version_is_part_of_snapshot_identity(self):
        environment_id = UUID("11111111-1111-4111-8111-111111111111")
        project_id = UUID("22222222-2222-4222-8222-222222222222")
        common = {
            "environment_id": environment_id,
            "project_id": project_id,
            "base_url": "https://staging.example.test",
            "network_policy": {"hosts": ["staging.example.test"]},
        }

        first = build_environment_snapshot(
            **common,
            secret_ref_version_id=UUID("33333333-3333-4333-8333-333333333333"),
        )
        rotated = build_environment_snapshot(
            **common,
            secret_ref_version_id=UUID("44444444-4444-4444-8444-444444444444"),
        )

        self.assertNotEqual(first.snapshot_hash, rotated.snapshot_hash)
        self.assertNotIn("password", first.model_dump_json().lower())


if __name__ == "__main__":
    unittest.main()
