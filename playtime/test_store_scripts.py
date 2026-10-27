"""Execute the real Lua/fenced store against a local Redis emulator, never production."""

import unittest

from cryptography.fernet import Fernet

from playtime.model import new_state
from playtime.serverless_store import RedisStore, SAVE

try:
    import fakeredis
except ImportError:
    fakeredis = None


@unittest.skipUnless(fakeredis, "Install requirements-test.txt for Redis/Lua integration tests")
class StoreScriptTests(unittest.TestCase):
    def setUp(self):
        self.redis = fakeredis.FakeRedis(decode_responses=True, version=(8, 4))
        key = Fernet.generate_key().decode()
        self.store = RedisStore("https://test.upstash.io", "private", key, "bot", "wrld_test")
        def rest_command(*parts):
            result = self.redis.execute_command(*parts)
            return "OK" if parts[0] in {"SET", "MSET"} and result is True else result

        self.store.command = rest_command
        self.state = new_state("bot", "wrld_test")
        self.store.initialize(self.state)

    def test_real_script_updates_state_and_preserves_previous_ciphertext(self):
        before = self.redis.get(self.store.prefix + ":state")
        self.assertTrue(self.store.acquire())
        state = self.store.load()
        state["players"]["usr_one"] = {"name": "One", "seconds": 600, "present": True}
        self.store.save(state)
        self.assertEqual(self.store.load()["players"]["usr_one"]["seconds"], 600)
        self.assertEqual(self.redis.get(self.store.prefix + ":previous"), before)
        self.store.release()
        self.assertFalse(self.redis.exists(self.store.prefix + ":lock"))

    def test_expired_owner_cannot_overwrite_state_or_release_a_new_lease(self):
        self.assertTrue(self.store.acquire())
        state = self.store.load()
        before = self.redis.get(self.store.prefix + ":state")
        self.redis.set(self.store.prefix + ":lock", "another-owner")
        with self.assertRaises(RuntimeError):
            self.store.save(state)
        self.assertEqual(self.redis.get(self.store.prefix + ":state"), before)
        self.assertFalse(self.redis.exists(self.store.prefix + ":previous"))
        self.store.release()
        self.assertEqual(self.redis.get(self.store.prefix + ":lock"), "another-owner")

    def test_loaded_ciphertext_check_still_rejects_unexpected_external_writes(self):
        self.assertTrue(self.store.acquire())
        state = self.store.load()
        self.redis.set(self.store.prefix + ":state", "changed-by-another-writer")
        with self.assertRaises(RuntimeError):
            self.store.save(state)
        self.assertEqual(self.redis.get(self.store.prefix + ":state"), "changed-by-another-writer")

    def test_capability_probe_creates_no_keys(self):
        before = self.redis.keys()
        self.assertTrue(self.store.native_release_supported())
        self.assertEqual(self.redis.keys(), before)

    def test_initialization_still_refuses_existing_hours(self):
        before = self.redis.get(self.store.prefix + ":state")
        with self.assertRaises(RuntimeError):
            self.store.initialize(self.state)
        self.assertEqual(self.redis.get(self.store.prefix + ":state"), before)

    def test_two_pre_and_post_request_saves_fit_monthly_command_budget(self):
        # Include EVAL and its two inner commands in each of the two fenced saves.
        per_sample = 1 + 1 + 2 * (1 + SAVE.count("redis.call(")) + 1
        self.assertEqual(per_sample, 9)
        self.assertLess((per_sample + 1) * 60 * 24 * 31, 500_000)


if __name__ == "__main__":
    unittest.main()
