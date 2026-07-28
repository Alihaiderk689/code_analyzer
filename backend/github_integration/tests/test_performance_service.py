from django.test import SimpleTestCase

from ..services.performance_service import PerformanceIssueType, find_performance_issues


class FindPerformanceIssuesTests(SimpleTestCase):
    def test_clean_code_yields_no_issues(self):
        code = 'def add(a, b):\n    return a + b\n'
        self.assertEqual(find_performance_issues(code), [])

    def test_range_len_detected(self):
        code = 'for i in range(len(items)):\n    print(items[i])\n'
        issues = find_performance_issues(code)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['type'], PerformanceIssueType.RANGE_LEN)
        self.assertEqual(issues[0]['source'], 'performance')
        self.assertEqual(issues[0]['severity'], 'medium')
        self.assertEqual(issues[0]['line'], 1)

    def test_requests_without_timeout_detected(self):
        code = 'resp = requests.get("https://api.example.com/data")\n'
        issues = find_performance_issues(code)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['type'], PerformanceIssueType.REQUESTS_NO_TIMEOUT)

    def test_requests_with_timeout_is_not_flagged(self):
        code = 'resp = requests.get("https://api.example.com/data", timeout=10)\n'
        self.assertEqual(find_performance_issues(code), [])

    def test_requests_post_put_patch_delete_all_detected(self):
        code = (
            'requests.post(url)\n'
            'requests.put(url)\n'
            'requests.patch(url)\n'
            'requests.delete(url)\n'
        )
        issues = find_performance_issues(code)
        self.assertEqual(len(issues), 4)
        self.assertTrue(all(i['type'] == PerformanceIssueType.REQUESTS_NO_TIMEOUT for i in issues))

    def test_select_star_detected_case_insensitively(self):
        code = 'query = "select * from users where active = true"\n'
        issues = find_performance_issues(code)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['type'], PerformanceIssueType.SELECT_STAR)
        self.assertEqual(issues[0]['severity'], 'low')

    def test_select_specific_columns_is_not_flagged(self):
        code = 'query = "SELECT id, name FROM users"\n'
        self.assertEqual(find_performance_issues(code), [])

    def test_blocking_sleep_detected(self):
        code = 'import time\ntime.sleep(5)\n'
        issues = find_performance_issues(code)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['type'], PerformanceIssueType.BLOCKING_SLEEP)
        self.assertEqual(issues[0]['line'], 2)

    def test_every_issue_has_explanation_and_remediation(self):
        code = 'for i in range(len(items)):\n    requests.get(url)\n    time.sleep(1)\n'
        issues = find_performance_issues(code)
        self.assertEqual(len(issues), 3)
        for issue in issues:
            self.assertTrue(issue['explanation'])
            self.assertTrue(issue['remediation'])
            self.assertTrue(issue['message'])

    def test_multiple_issues_on_different_lines_all_reported(self):
        code = (
            'for i in range(len(items)):\n'
            '    resp = requests.get(url)\n'
            '    time.sleep(1)\n'
        )
        issues = find_performance_issues(code)
        lines = sorted(i['line'] for i in issues)
        self.assertEqual(lines, [1, 2, 3])
