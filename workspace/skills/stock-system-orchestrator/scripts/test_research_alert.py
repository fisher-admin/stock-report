import importlib.util
import inspect
import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path


class ResearchAlertTests(unittest.TestCase):
    def test_persistent_failure_stops_at_three_attempts(self):
        module = self.module()
        now = [100.0]
        send = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"error":"offline"}', ''))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module.time, 'time', side_effect=lambda:now[0]):
            event = module.notify_failure(root=Path(tmp), run_id='run-1', trade_date='20260911', stage='probe', error_kind='failure', target=module.EXISTING_STOCK_TARGET, sender=send)
            events = module.retry_failures([event], root=Path(tmp), target=module.EXISTING_STOCK_TARGET,
                deadline=350, clock=lambda:now[0], sleep=lambda seconds:now.__setitem__(0,now[0]+seconds), sender=send)
            self.assertEqual(send.call_count, 3)
            self.assertEqual(events[0]['status'], 'failed')
            self.assertEqual(events[0]['attempts'], 3)

    def test_last_attempt_is_clamped_to_remaining_final_budget(self):
        module = self.module()
        now = [100.0]
        send = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"error":"offline"}', ''))
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module.time, 'time', side_effect=lambda:now[0]):
            event = module.notify_failure(root=Path(tmp), run_id='run-1', trade_date='20260911', stage='probe', error_kind='failure', target=module.EXISTING_STOCK_TARGET, sender=send)
            now[0] = 160
            module.retry_failures([event], root=Path(tmp), target=module.EXISTING_STOCK_TARGET,
                deadline=167, clock=lambda:now[0], sleep=lambda seconds:now.__setitem__(0,now[0]+seconds), sender=send)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(send.call_args.kwargs['timeout'], 5)

    def test_failed_delivery_retries_with_backoff_within_final_deadline(self):
        module = self.module()
        self.assertTrue(hasattr(module, 'retry_failures'), 'final bounded retry path is missing')
        now = [100.0]
        calls = []
        def send(command, **kwargs):
            calls.append((now[0], kwargs['timeout']))
            payload = {'error': 'unavailable'} if len(calls) < 3 else {'success': True, 'message_id': '123'}
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), '')
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module.time, 'time', side_effect=lambda: now[0]):
            root = Path(tmp)
            event = module.notify_failure(root=root, run_id='run-1', trade_date='20260911', stage='probe', error_kind='failure', target=module.EXISTING_STOCK_TARGET, sender=send)
            events = module.retry_failures([event], root=root, target=module.EXISTING_STOCK_TARGET,
                deadline=350, clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), sender=send)
            self.assertEqual(events[0]['status'], 'accepted')
            self.assertEqual([call[0] for call in calls], [100, 160, 220])

    def test_retry_does_not_outlive_deadline_or_resend_unknown(self):
        module = self.module()
        self.assertTrue(hasattr(module, 'retry_failures'), 'final bounded retry path is missing')
        now = [100.0]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(module.time, 'time', side_effect=lambda: now[0]):
            kwargs = dict(root=Path(tmp), run_id='run-1', trade_date='20260911', stage='probe', error_kind='failure', target=module.EXISTING_STOCK_TARGET)
            send = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"error":"offline"}', ''))
            event = module.notify_failure(**kwargs, sender=send)
            module.retry_failures([event], root=Path(tmp), target=module.EXISTING_STOCK_TARGET,
                deadline=130, clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), sender=send)
            self.assertLessEqual(now[0], 130)
            self.assertEqual(send.call_count, 1)
            module.retry_failures([{**event, 'status':'unknown'}], root=Path(tmp), target=module.EXISTING_STOCK_TARGET,
                deadline=350, clock=lambda: now[0], sleep=lambda seconds: None, sender=send)
            self.assertEqual(send.call_count, 1)

    def test_no_send_budget_still_persists_pending_event(self):
        module = self.module()
        self.assertIn('timeout_s', inspect.signature(module.notify_failure).parameters)
        with tempfile.TemporaryDirectory() as tmp:
            send = mock.Mock()
            event = module.notify_failure(root=Path(tmp), run_id='run-1', trade_date='20260911', stage='probe', error_kind='failure', target=module.EXISTING_STOCK_TARGET, sender=send, timeout_s=0)
            self.assertEqual(event['status'], 'pending')
            send.assert_not_called()

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('research_alert'), 'durable immediate alert missing')
        import research_alert
        return research_alert

    def test_receipt_requires_success_message_id_and_not_skipped(self):
        module = self.module()
        for payload, status in [
            ({'success': True, 'message_id': '123'}, 'accepted'),
            ({'success': True, 'skipped': True}, 'skipped'),
            ({'success': True}, 'unknown'),
            ({'success': 'true', 'message_id': '123'}, 'unknown'),
            ({'error': 'unavailable'}, 'failed')]:
            self.assertEqual(module.delivery_status(payload), status)

    def test_event_saved_before_send_and_accepted_not_resent(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []
            def send(command, **kwargs):
                events = list(root.glob('*.json'))
                self.assertEqual(len(events), 1)
                event = json.loads(events[0].read_text())
                self.assertEqual(event['status'], 'sending')
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, json.dumps({'success': True, 'message_id': '123'}), '')
            kwargs = dict(root=root, run_id='run-1', trade_date='20260911', stage='factorAttribution', error_kind='timeout', target='discord:1492058190492467230', sender=send)
            self.assertEqual(module.notify_failure(**kwargs)['status'], 'accepted')
            self.assertEqual(module.notify_failure(**kwargs)['status'], 'accepted')
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][-1], module.PUBLIC_FAILURE_NOTICE)
            self.assertNotIn('run-1', ' '.join(calls[0]))
            self.assertNotIn('20260911', ' '.join(calls[0]))

    def test_timeout_is_unknown_and_not_blindly_retried(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def send(command, **kwargs):
                calls.append(command)
                raise subprocess.TimeoutExpired(command, 20)
            kwargs = dict(root=Path(tmp), run_id='run-1', trade_date='20260911', stage='factorAttribution', error_kind='timeout', target='discord:1492058190492467230', sender=send)
            self.assertEqual(module.notify_failure(**kwargs)['status'], 'unknown')
            module.notify_failure(**kwargs)
            self.assertEqual(len(calls), 1)

    def test_no_target_records_pending_without_sending(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp:
            result = module.notify_failure(root=Path(tmp), run_id='run-1', trade_date='20260911', stage='probe', error_kind='failure', target=None)
            self.assertEqual(result['status'], 'pending')

    def test_new_destination_is_refused(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            module.notify_failure(root=Path(tmp), run_id='run-1', trade_date='20260911', stage='probe', error_kind='failure', target='discord:other')
