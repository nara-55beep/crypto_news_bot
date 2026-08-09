import contextlib
import io
import unittest

import telegram_listener


class _FakeClient:
    def __init__(self, authorized):
        self.authorized = authorized
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return self.authorized

    async def disconnect(self):
        self.disconnected = True


class TestNonInteractiveTelegramStartup(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_session_connects_without_interactive_start(self):
        client = _FakeClient(True)
        self.assertTrue(await telegram_listener._connect_authorized(client))
        self.assertTrue(client.connected)
        self.assertFalse(client.disconnected)

    async def test_unauthorized_session_disables_without_prompting(self):
        client = _FakeClient(False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(await telegram_listener._connect_authorized(client))
        self.assertTrue(client.connected)
        self.assertTrue(client.disconnected)
        self.assertIn("telegram_login.py", output.getvalue())


if __name__ == "__main__":
    unittest.main()
