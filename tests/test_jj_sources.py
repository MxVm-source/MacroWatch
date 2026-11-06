import unittest
from datetime import datetime, timezone

from bot.modules import jj_sources as src


def dt(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def row(title='Non-Farm Employment Change', **kw):
    return dict(title=title, country='USD', impact='High',
                date='2026-11-06T08:30:00-05:00', forecast='150K', previous='100K', **kw)


class SourcesTests(unittest.TestCase):
    def test_filter_us_relevance_and_reject_naive_time(self):
        rows = [row(), row('Crude Oil Inventories'),
                {**row(), 'country': 'CAD'}, {**row(), 'date': '2026-11-06T08:30:00'},
                {**row('Retail Sales m/m'), 'impact': 'Medium'},
                {**row(), 'date': 'invalid'}, {**row(), 'impact': 'Low'}]
        events = src.normalize_calendar(rows)
        self.assertEqual([x['title'] for x in events],
                         ['Non-Farm Employment Change', 'Retail Sales m/m'])
        self.assertEqual(events[0]['forecast'], '150K')
        self.assertEqual(events[0]['start'], dt('2026-11-06T13:30:00+00:00'))

    def test_dedup_and_no_actual_inference(self):
        events = src.normalize_calendar([row(), row()])
        self.assertEqual(len(events), 1)
        self.assertNotIn('actual', events[0])

    def test_official_schedule_overrides_conflict_without_carrying_consensus(self):
        ics = ('BEGIN:VCALENDAR\nBEGIN:VEVENT\n'
               'DTSTART;TZID=US-Eastern:20261107T083000\n'
               'SUMMARY:Employment Situation\nEND:VEVENT\nEND:VCALENDAR')
        official = src.parse_bls_calendar(ics)
        events = src.merge_official(src.normalize_calendar([row()]), official)
        self.assertFalse(any(e['start'].date().isoformat() == '2026-11-06' for e in events))
        self.assertTrue(all(not e.get('forecast') for e in events))
        self.assertTrue(all(e['schedule_source'] == src.BLS_CALENDAR for e in events))

    def test_bls_atom_confirms_only_same_release_day_and_metric(self):
        atom = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title>Payroll employment rises by 165,000 in October; unemployment rate 4.1%</title>
        <link href="https://www.bls.gov/news.release/archives/empsit_11062026.htm"/>
        <updated>2026-11-06T07:50:00-05:00</updated>
        <content>Total nonfarm payroll employment rose by 165,000 in October,
        and the unemployment rate was 4.1 percent.</content></entry></feed>'''
        items = src.parse_feed(atom, 'BLS')
        event = src.normalize_calendar([row()])[0]
        result = src.match_release(event, items)
        self.assertEqual(result['actual'], '165K')
        self.assertEqual(result['unit'], 'K')
        self.assertEqual(src.compare_values(result['actual'], event['forecast']), '+15K')
        event['start'] = dt('2026-12-04T13:30:00+00:00')
        self.assertIsNone(src.match_release(event, items))

    def test_no_false_confirmation_from_wrong_agency_or_unrelated_release(self):
        event = src.normalize_calendar([row()])[0]
        item = dict(title='Payroll employment rises by 165,000', body='',
                    url='https://example.org/news', published=event['start'], source='BLS')
        self.assertIsNone(src.match_release(event, [item]))

    def test_feed_requires_item_date_not_feed_timestamp(self):
        self.assertEqual(src.parse_feed('<rss><channel><pubDate>Fri, 06 Nov 2026 13:30:00 GMT</pubDate>'
                                      '<item><title>undated</title><link>https://www.bea.gov/a</link>'
                                      '</item></channel></rss>', 'BEA'), [])

    def test_values_only_compare_identical_units(self):
        self.assertIsNone(src.compare_values('165K', '0.2%'))
        self.assertEqual(src.compare_values('0.3%', '0.2%'), '+0.1 point(s)')
        self.assertIsNone(src.compare_values('unavailable', '150K'))

    def test_jolts_calendar_does_not_move_jobless_claims(self):
        claims = src.normalize_calendar([row('Unemployment Claims')])
        official = src.parse_bls_calendar('BEGIN:VCALENDAR\nBEGIN:VEVENT\n'
            'DTSTART;TZID=US-Eastern:20261103T100000\n'
            'SUMMARY:Job Openings and Labor Turnover Survey\nEND:VEVENT\nEND:VCALENDAR')
        events = src.merge_official(claims, official)
        self.assertEqual(next(e for e in events if e['title'] == 'Unemployment Claims')['start'],
                         claims[0]['start'])

    def test_market_sessions_and_dst(self):
        self.assertIsNone(src.session_open('2026-11-26'))
        self.assertIsNone(src.session_open('2026-11-07'))
        self.assertEqual(src.session_open('2026-11-06').astimezone(src.BRUSSELS).hour, 15)
        self.assertEqual(src.session_open('2026-10-28').astimezone(src.BRUSSELS).hour, 14)

    def test_adp_does_not_get_removed_by_bls_schedule(self):
        adp = src.normalize_calendar([{**row('ADP Non-Farm Employment Change'),
                                      'date': '2026-11-04T08:15:00-05:00'}])
        official = src.parse_bls_calendar('BEGIN:VCALENDAR\nBEGIN:VEVENT\n'
            'DTSTART;TZID=US-Eastern:20261106T083000\nSUMMARY:Employment Situation\nEND:VEVENT\nEND:VCALENDAR')
        self.assertTrue(any('ADP' in e['title'] for e in src.merge_official(adp, official)))

    def test_core_cpi_cannot_become_headline_actual(self):
        event = src.normalize_calendar([row('CPI m/m')])[0]
        item = dict(title='CPI for all items unchanged', source='BLS CPI',
                    url='https://www.bls.gov/news.release/archives/cpi_11062026.htm', published=event['start'],
                    body='The Consumer Price Index was unchanged in October. The index for all items less food '
                         'and energy rose 0.3 percent, seasonally adjusted.')
        self.assertEqual(src.match_release(event, [item])['actual'], '')

    def test_annual_gdp_update_does_not_confirm_advance_estimate(self):
        event = src.normalize_calendar([row('Advance GDP q/q')])[0]
        item = dict(title='Gross Domestic Product: Annual Update', body='', source='BEA',
                    url='https://www.bea.gov/news/test', published=event['start'])
        self.assertIsNone(src.match_release(event, [item]))

    def test_mojibake_bom_feed_is_handled(self):
        text = 'ï»¿<?xml version="1.0"?><rss><channel></channel></rss>'
        self.assertEqual(src.parse_feed(text, 'Fed'), [])

    def test_old_bls_archive_retimestampped_today_is_not_current_release(self):
        event = src.normalize_calendar([row()])[0]
        item = dict(title='Payroll employment rises by 165,000', body='', source='BLS',
            url='https://www.bls.gov/news.release/archives/empsit_10022026.htm', published=event['start'])
        self.assertIsNone(src.match_release(event, [item]))

    def test_future_timestamp_not_yet_published(self):
        event = src.normalize_calendar([row()])[0]
        item = dict(title='Payroll employment rises by 165,000', body='', source='BLS',
            url='https://www.bls.gov/news.release/archives/empsit_11062026.htm',
            published=dt('2026-11-06T16:00:00+00:00'))
        self.assertIsNone(src.match_release(event, [item], now=dt('2026-11-06T13:31:00+00:00')))


if __name__ == '__main__':
    unittest.main()
