import tempfile
import unittest
from unittest.mock import Mock, patch
from bot.modules.jjwatch import Engine
from bot import group_maintenance as groups


class GroupCleanupTests(unittest.TestCase):
    def test_verified_destinations_updated_once_without_posting_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Engine(directory, sender=lambda _: True)
            private = {'id': -1, 'title': 'INFINEX Capital MacroWatch', 'type': 'supergroup'}
            public = {'id': -2, 'title': 'INFINEX CAPITAL', 'username': 'macrowatchalpha', 'type': 'supergroup'}
            responses = [Mock(json=lambda: {'ok': True, 'result': private}),
                         Mock(json=lambda: {'ok': True, 'result': public}),
                         Mock(json=lambda: {'ok': True}), Mock(json=lambda: {'ok': True})]
            with patch.object(groups, 'API_BASE', 'https://api.telegram.org/bottest'), \
                 patch.object(groups, 'CHAT_ID', '-1'), \
                 patch.dict(groups.os.environ, {'PUBLIC_CHAT_ID': '@macrowatchalpha'}), \
                 patch.object(groups.requests, 'post', side_effect=responses) as post:
                groups.update_descriptions(worker)
                groups.update_descriptions(worker)
                urls = [call.args[0] for call in post.call_args_list]
            self.assertEqual(len(urls), 4)
            self.assertEqual(sum(url.endswith('/setChatDescription') for url in urls), 2)
            self.assertFalse(any(url.endswith('/sendMessage') for url in urls))

    def test_same_canonical_group_rejected_even_with_different_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Engine(directory, sender=lambda _: True)
            chat = {'id': -1, 'title': 'INFINEX Capital MacroWatch', 'username': 'macrowatchalpha'}
            with patch.object(groups, 'API_BASE', 'test'), patch.object(groups, 'CHAT_ID', '-1'), \
                 patch.dict(groups.os.environ, {'PUBLIC_CHAT_ID': '@macrowatchalpha'}), \
                 patch.object(groups.requests, 'post', return_value=Mock(json=lambda: {'ok': True, 'result': chat})) as post:
                groups.update_descriptions(worker)
            self.assertTrue(all(call.args[0].endswith('/getChat') for call in post.call_args_list))


if __name__ == '__main__':
    unittest.main()
