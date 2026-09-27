"""Offline behavior tests; no API keys, network calls, or message delivery."""
import io
import json
import struct
import unittest
import zipfile
import zlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from bs4 import BeautifulSoup
from openai import RateLimitError
from pydantic import ValidationError

import main as m


def summary(**changes):
    data = dict(is_target=True, target_reason="서울 청년 모집", seoul_eligible="yes",
                total_units=100, supply_scope="서울 전체 공급 호수", regions=["서울 강서구", "서울 관악구"],
                deposit={"min_krw": 1000000, "max_krw": 2000000, "basis": "기본 보증금"},
                monthly_rent={"min_krw": 100000, "max_krw": 250000, "basis": "기본 월세"},
                application_period="2026.10.01 ~ 10.05", overview="청년 임대주택 모집이에요.", notes=[])
    data.update(changes)
    return m.HousingSummary(**data)


POST = m.Announcement("LH", "test-1", "서울 청년 매입임대 모집", "https://apply.lh.or.kr/test", "2026-09-27", "서울특별시")


def openai_429(code, retry_after=None):
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(429, request=request, headers=headers)
    return RateLimitError("private API response", response=response, body={"code": code, "type": code})


def parsed_response():
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
        message=SimpleNamespace(refusal=None, parsed=summary()))])


class ParsingTests(unittest.TestCase):
    def test_spacing_and_title_filters(self):
        self.assertTrue(m.keyword_match("2026년 청년 매입 임대주택 모집"))
        self.assertTrue(m.keyword_match("행복주택 입주자 모집"))
        self.assertFalse(m.keyword_match("청년매입임대 당첨자 발표"))
        self.assertFalse(m.keyword_match("직원 채용공고"))

    def test_lh_observed_data_attributes_and_badge(self):
        html = '''<table><tr><td class="bbs_tit"><a class="wrtancInfoBtn"
          data-id1="2015122300020759" data-id2="03" data-id3="13" data-id4="26">
          <span>서울 청년 매입임대<em class="day">1일전</em></span></a></td>
          <td class="col2">서울특별시</td><td>2026.09.17</td></tr></table>'''
        p = m.parse_lh_list(BeautifulSoup(html, "html.parser"))[0]
        self.assertEqual(p.post_id, "2015122300020759")
        self.assertEqual(p.title, "서울 청년 매입임대")
        self.assertIn("aisTpCd=26", p.url)
        self.assertEqual(p.published_at, "2026-09-17")

    def test_sh_preserves_real_board_id(self):
        html = '''<table><tr><td><a href="/main/lay2/program/S1T294C295/www/brd/m_241/view.do?seq=123">
          청년 안심주택 모집</a></td><td>2026-09-27</td></tr></table>'''
        posts = m.parse_sh_list(BeautifulSoup(html, "html.parser"), m.TARGET_SITES[2]["url"])
        self.assertEqual(posts[0].post_id, "241:123")
        self.assertIn("m_241", posts[0].url)

    def test_sh_js_link_variant(self):
        html = '<tr><td><a href="#" onclick="fnView(\'456\');">청년주택</a></td></tr>'
        p = m.parse_sh_list(BeautifulSoup(html, "html.parser"), m.TARGET_SITES[2]["url"])[0]
        self.assertEqual(p.post_id, "247:456")

    def test_lh_js_attachments_and_direct_sh_files(self):
        soup = BeautifulSoup('''<a href="javascript:fileDownLoad('123');">공고문.pdf</a>
          <a href="javascript:fileDownLoad('124');">공고문.hwpx</a>''', "html.parser")
        attachments, errors = m.attachments_from(soup, POST)
        self.assertEqual(len(attachments), 2)
        self.assertTrue(attachments[0].url.endswith("fileid=123"))
        self.assertFalse(errors)
        sh = m.Announcement("SH", "241:1", "제목", "https://www.i-sh.co.kr/main/view.do")
        soup = BeautifulSoup('<a href="/files/download.do?seq=1">공고.hwp</a>', "html.parser")
        self.assertEqual(len(m.attachments_from(soup, sh)[0]), 1)

    def test_no_footer_region_false_positive(self):
        p = m.Announcement("LH", "2", "청년매입임대", POST.url, region="부산광역시")
        self.assertFalse(m.region_candidate(p))
        self.assertTrue(m.region_candidate(m.Announcement("LH", "3", "청년전세임대", POST.url, region="전국")))

    def test_other_provinces_suffix_is_not_evidence_of_seoul(self):
        post = m.Announcement("LH", "2015122300020682",
            "대구혁신10, 경산하양3 행복주택 입주자격완화 선계약 후검증 동호지정 입주자 모집",
            POST.url, "2026-09-03", "대구광역시 외")
        self.assertTrue(m.keyword_match(post.title))
        self.assertFalse(m.region_candidate(post))
        self.assertFalse(m.region_candidate(post, {"2015122300020759"}))

    def test_multi_region_post_can_be_verified_by_seoul_search(self):
        post = m.Announcement("LH", "multi", "청년 매입임대 모집", POST.url, region="경기도 외")
        self.assertFalse(m.region_candidate(post))
        self.assertTrue(m.region_candidate(post, {"multi"}))

    def test_seoul_nationwide_and_capital_region_candidates_remain(self):
        for region in ["서울특별시", "서울특별시 외", "전국", "수도권"]:
            with self.subTest(region=region):
                self.assertTrue(m.region_candidate(m.Announcement("LH", "id", "청년주택", POST.url, region=region)))

    def test_seoul_search_stops_on_single_page(self):
        html = '''<select id="cnpCd"><option value="11" selected>서울특별시</option></select>
          <div class="bbs_pagerA"><strong class="bbs_pge_num">1</strong></div>'''
        web = Mock()
        web.soup.return_value = BeautifulSoup(html, "html.parser")
        crawler = m.Crawler(web, m.Settings())
        with patch.object(m, "parse_lh_list", return_value=[POST]):
            self.assertEqual(crawler.listings(m.TARGET_SITES[0], region_code="11"), [POST])
        self.assertEqual(web.soup.call_count, 1)
        self.assertEqual(web.soup.call_args.args[1]["cnpCd"], "11")

    def test_ignored_region_search_is_not_accepted(self):
        web = Mock()
        web.soup.return_value = BeautifulSoup('<select id="cnpCd"><option value="" selected>전국</option></select>', "html.parser")
        with self.assertRaises(m.SiteError):
            m.Crawler(web, m.Settings()).listings(m.TARGET_SITES[0], region_code="11")

    def test_off_region_candidate_does_not_consume_preview_limit(self):
        excluded = m.Announcement("LH", "excluded", "대구 경산 행복주택 모집", POST.url, "2026-09-03", "대구광역시 외")
        web, crawler = Mock(), Mock()
        crawler.listings.side_effect = [[excluded, POST], [POST]]
        crawler.detail.return_value = m.Document("서울 공고 본문")
        args = SimpleNamespace(dry_run=True, source="LH", output_dir=None)
        with patch.object(m, "PublicWeb", return_value=web), patch.object(m, "Crawler", return_value=crawler):
            result = m.run(args, m.Settings(max_posts=1))
        self.assertEqual(result, 0)
        crawler.detail.assert_called_once_with(POST)

    def test_private_or_foreign_attachment_rejected(self):
        for url in ["http://127.0.0.1/", "file:///etc/passwd", "https://evil.test/a", "https://apply.lh.or.kr:8080/a"]:
            with self.assertRaises(m.SiteError):
                m.safe_url(url)

    def test_hwp_records_and_binary_controls(self):
        payload = "공급 100호".encode("utf-16le") + struct.pack("<8H", 9, 777, 888, 999, 111, 222, 333, 9) + "월세".encode("utf-16le")
        raw = struct.pack("<I", 67 | (len(payload) << 20)) + payload
        self.assertEqual(m.hwp_records(raw), "공급 100호\t월세")
        raw_extended = struct.pack("<II", 67 | (0xFFF << 20), len(payload)) + payload
        self.assertEqual(m.hwp_records(raw_extended), "공급 100호\t월세")
        with self.assertRaises(m.PipelineError):
            m.hwp_records(raw[:-1])

    def test_hwp_inflate_limit(self):
        obj = zlib.compressobj(wbits=-15)
        compressed = obj.compress(b"a" * 500) + obj.flush()
        with patch.object(m, "MAX_EXPANDED_BYTES", 100):
            with self.assertRaises(m.PipelineError):
                m.bounded_inflate(compressed)

    def test_hwpx_sections_numeric_order(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as z:
            for n in [10, 2]:
                z.writestr(f"Contents/section{n}.xml", f'<root xmlns:p="urn:test"><p:p><p:t>문단{n}</p:t></p:p></root>')
        self.assertEqual(m.extract_hwpx(data.getvalue()), "문단2\n문단10")

    def test_attachment_failure_does_not_stop_later_attachment(self):
        doc = m.Document("본문", [m.Attachment("bad.pdf", POST.url), m.Attachment("good.pdf", POST.url + "2")])
        web = Mock()
        web.get_bytes.side_effect = [b"<html>bad</html>", b"%PDF-1.7 good"]
        with patch.object(m, "extract_pdf", return_value=("공급 20호", [])):
            m.collect_attachments(doc, POST, web, m.Settings())
        self.assertEqual(len(doc.warnings), 1)
        self.assertIn("공급 20호", doc.texts[0])

    def test_split_preserves_all_input(self):
        text = ("서울 공급 10호\n" * 1000) + "마지막 공급정보"
        chunks = m.split_text(text, 2000)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(c) <= 2000 for c in chunks))

    def test_document_chunks_preserve_table_unit_context(self):
        doc = m.Document("본문", texts=["[첨부파일: 목록.xlsx]\n주소 | 보증금(만원) | 월세(원)\n" + "서울 강서구 | 100 | 123456\n" * 300])
        chunks = m.document_chunks(POST, doc, 2000)
        self.assertGreater(len(chunks), 2)
        for chunk in chunks[2:]:
            self.assertIn("보증금(만원)", chunk)
            self.assertIn("반복 문맥", chunk)
        self.assertTrue(all(len(c) <= 2000 for c in chunks))

    def test_viewer_download_link_is_not_an_attachment(self):
        soup = BeautifulSoup('<a href="https://www.hancom.com/cs_center/csDownload.do">한컴오피스 뷰어</a>', "html.parser")
        files, warnings = m.attachments_from(soup, POST)
        self.assertEqual((files, warnings), ([], []))

    def test_llm_chunk_limit_fails_without_silent_truncation(self):
        summarizer = object.__new__(m.Summarizer)
        summarizer.settings = m.Settings(chunk_chars=2000, max_chunks=1)
        summarizer.request = Mock()
        with self.assertRaises(m.PipelineError):
            summarizer.summarize(POST, m.Document("한글" * 3000))
        summarizer.request.assert_not_called()

    def test_large_summary_maps_all_chunks_then_reduces(self):
        summarizer = object.__new__(m.Summarizer)
        summarizer.settings = m.Settings(chunk_chars=2000, max_chunks=12)
        summarizer.request = Mock(return_value=summary())
        doc = m.Document("본문" * 1500, texts=["[첨부] 공급 금액 마지막 원문"])
        chunks = m.document_chunks(POST, doc, 2000)
        summarizer.summarize(POST, doc)
        self.assertEqual(summarizer.request.call_count, len(chunks) + 1)
        self.assertTrue(any("공급 금액 마지막 원문" in call.args[0] for call in summarizer.request.call_args_list))

    def test_xlsx_keeps_address_units_and_zero(self):
        book = m.openpyxl.Workbook()
        book.active.append(["공급지역", "보증금(원)", "월세(원)"])
        book.active.append(["서울 강서구", 1000000, 0])
        data = io.BytesIO()
        book.save(data); book.close()
        text = m.extract_xlsx(data.getvalue())
        self.assertIn("공급지역 | 보증금(원) | 월세(원)", text)
        self.assertIn("서울 강서구 | 1000000 | 0", text)

    def test_structured_json_rejects_invalid_values(self):
        with self.assertRaises(ValidationError):
            summary(deposit={"min_krw": 200, "max_krw": 100, "basis": "오류"})
        with self.assertRaises(ValidationError):
            summary(total_units="100")
        with self.assertRaises(ValidationError):
            summary(total_units=-1)

    def test_telegram_zero_unknown_and_url_last(self):
        s = summary(total_units=None, deposit={"min_krw": 0, "max_krw": 0, "basis": "면제"})
        msg = m.telegram_message(POST, s, ["읽기 실패"])
        self.assertIn("보증금: 0원", msg)
        self.assertIn("총 공급: 원문 확인 필요", msg)
        self.assertEqual(msg.splitlines()[-1], POST.url)
        self.assertLessEqual(len(msg.encode("utf-16le")) // 2, 4096)

    def test_large_message_stays_bounded(self):
        s = summary(regions=["아주 긴 지역명" * 30] * 300, notes=["설명" * 1000] * 30, overview="한글" * 1000)
        self.assertLessEqual(len(m.telegram_message(POST, s, []).encode("utf-16le")) // 2, 4096)

    def test_page_change_failure_is_not_empty_success(self):
        crawler = m.Crawler(Mock(), m.Settings())
        with patch.object(m, "parse_lh_list", return_value=[]):
            crawler.web.soup.return_value = BeautifulSoup("<h1>접근 오류</h1>", "html.parser")
            with self.assertRaises(m.SiteError):
                crawler.listings(m.TARGET_SITES[0])


class SummarizerRetryTests(unittest.TestCase):
    def setUp(self):
        jitter = patch("main.random.uniform", return_value=0.0)
        jitter.start()
        self.addCleanup(jitter.stop)

    def make_summarizer(self, responses, interval=0):
        summarizer = object.__new__(m.Summarizer)
        summarizer.settings = m.Settings(llm_min_interval=interval)
        summarizer.client = Mock()
        summarizer.client.chat.completions.parse.side_effect = responses
        summarizer.last_request_at = None
        return summarizer

    @patch("main.time.sleep")
    def test_retry_after_is_honored_and_result_is_parsed(self, sleep):
        summarizer = self.make_summarizer([openai_429("rate_limit_exceeded", "75"), parsed_response()])
        self.assertEqual(summarizer.request("본문").total_units, 100)
        sleep.assert_called_once_with(75.0)
        self.assertEqual(summarizer.client.chat.completions.parse.call_count, 2)

    @patch("main.time.sleep")
    def test_quota_exhaustion_does_not_retry_or_expose_response(self, sleep):
        summarizer = self.make_summarizer([openai_429("insufficient_quota")])
        with self.assertRaises(m.LLMQuotaError) as error:
            summarizer.request("본문")
        self.assertIn("insufficient_quota", str(error.exception))
        self.assertNotIn("private API response", str(error.exception))
        self.assertEqual(summarizer.client.chat.completions.parse.call_count, 1)
        sleep.assert_not_called()

    @patch("main.time.sleep")
    def test_new_billing_code_is_classified_by_error_type(self, sleep):
        error = openai_429("new_billing_code")
        error.type = "insufficient_quota"
        summarizer = self.make_summarizer([error])
        with self.assertRaises(m.LLMQuotaError):
            summarizer.request("본문")
        sleep.assert_not_called()

    @patch("main.time.sleep")
    def test_temporary_limit_has_bounded_backoff(self, sleep):
        summarizer = self.make_summarizer([openai_429("rate_limit_exceeded") for _ in range(4)])
        with self.assertRaises(m.LLMRateDeferred):
            summarizer.request("본문")
        self.assertEqual(summarizer.client.chat.completions.parse.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [60.0, 120.0, 240.0])

    @patch("main.time.sleep")
    def test_long_server_retry_after_defers_without_sleep(self, sleep):
        summarizer = self.make_summarizer([openai_429("rate_limit_exceeded", "601")])
        with self.assertRaises(m.LLMRateDeferred):
            summarizer.request("본문")
        self.assertEqual(summarizer.client.chat.completions.parse.call_count, 1)
        sleep.assert_not_called()

    @patch("main.time.monotonic", side_effect=[100.0, 102.0, 115.0])
    @patch("main.time.sleep")
    def test_sequential_chunks_are_paced(self, sleep, monotonic):
        summarizer = self.make_summarizer([parsed_response(), parsed_response()], interval=15)
        summarizer.request("첫 조각")
        summarizer.request("둘째 조각")
        sleep.assert_called_once_with(13.0)


class RunLimitTests(unittest.TestCase):
    @patch("main.require_secrets")
    @patch("main.Telegram")
    @patch("main.Summarizer")
    @patch("main.Repository")
    @patch("main.Crawler")
    @patch("main.PublicWeb")
    @patch("main.process_post", return_value="quota_exhausted")
    def test_quota_stops_before_next_paid_post(self, process, web, crawler, repo, summarizer, telegram, secrets):
        repo.return_value.pending.return_value = []
        crawler.return_value.listings.return_value = [POST,
            m.Announcement("LH", "test-2", "서울 청년 매입임대 추가 모집",
                "https://apply.lh.or.kr/another", "2026-09-28", "서울특별시")]
        args = SimpleNamespace(dry_run=False, source="LH", output_dir=None)
        with patch.dict(m.os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "1"}):
            self.assertEqual(m.run(args, m.Settings()), 1)
        self.assertEqual(process.call_count, 1)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.repo = Mock()
        self.repo.exists.return_value = False
        self.repo.claim.return_value = True
        self.repo.begin.return_value = True
        self.crawler = Mock()
        self.crawler.detail.return_value = m.Document("청년 임대주택 공고 서울 공급")
        self.summarizer = Mock()
        self.summarizer.summarize.return_value = summary()
        self.telegram = Mock()
        self.telegram.send.return_value = 42

    def process(self):
        return m.process_post(POST, self.repo, self.crawler, self.summarizer, self.telegram, m.Settings())

    def test_existing_skips_download_llm_and_telegram(self):
        self.repo.exists.return_value = True
        self.assertEqual(self.process(), "existing")
        self.crawler.detail.assert_not_called()
        self.summarizer.summarize.assert_not_called()
        self.telegram.send.assert_not_called()

    def test_concurrent_claim_loser_never_sends(self):
        self.repo.claim.return_value = False
        self.assertEqual(self.process(), "reserved")
        self.telegram.send.assert_not_called()

    def test_commit_only_after_success(self):
        calls = []
        self.repo.begin.side_effect = lambda *a: calls.append("begin") or True
        self.telegram.send.side_effect = lambda *a: calls.append("send") or 42
        self.repo.complete.side_effect = lambda *a: calls.append("commit")
        self.assertEqual(self.process(), "sent")
        self.assertEqual(calls, ["begin", "send", "commit"])

    def test_llm_failure_remains_retryable(self):
        self.summarizer.summarize.side_effect = ValueError("bad JSON")
        self.assertEqual(self.process(), "failed")
        self.assertEqual(self.repo.mark.call_args.args[2], "failed")
        self.telegram.send.assert_not_called()

    def test_quota_error_stays_retryable_but_stops_run(self):
        self.summarizer.summarize.side_effect = m.LLMQuotaError("insufficient_quota")
        self.assertEqual(self.process(), "quota_exhausted")
        self.assertEqual(self.repo.mark.call_args.args[2], "failed")
        self.telegram.send.assert_not_called()

    def test_rate_limit_error_stays_retryable_but_stops_run(self):
        self.summarizer.summarize.side_effect = m.LLMRateDeferred("rate limit")
        self.assertEqual(self.process(), "rate_limited")
        self.assertEqual(self.repo.mark.call_args.args[2], "failed")
        self.telegram.send.assert_not_called()

    def test_telegram_explicit_rejection_retryable(self):
        self.telegram.send.side_effect = m.DeliveryRejected("429")
        self.assertEqual(self.process(), "failed")
        self.assertEqual(self.repo.mark.call_args.args[2], "failed")
        self.repo.complete.assert_not_called()

    def test_timeout_never_records_success_or_retries(self):
        self.telegram.send.side_effect = m.DeliveryUncertain("timeout")
        self.assertEqual(self.process(), "failed")
        self.assertEqual(self.repo.mark.call_args.args[2], "uncertain")
        self.repo.complete.assert_not_called()
        self.assertEqual(self.telegram.send.call_count, 1)

    def test_post_send_db_failure_requires_manual_reconciliation(self):
        self.repo.complete.side_effect = RuntimeError("database unavailable")
        self.assertEqual(self.process(), "failed")
        self.assertEqual(self.repo.mark.call_args.args[2], "uncertain")
        self.assertEqual(self.telegram.send.call_count, 1)

    def test_lease_loss_never_sends(self):
        self.repo.begin.return_value = False
        self.assertEqual(self.process(), "failed")
        self.telegram.send.assert_not_called()

    def test_unknown_region_not_notified(self):
        self.summarizer.summarize.return_value = summary(seoul_eligible="unknown")
        self.assertEqual(self.process(), "failed")
        self.telegram.send.assert_not_called()

    def test_wrong_region_marked_skipped(self):
        self.summarizer.summarize.return_value = summary(seoul_eligible="no")
        self.assertEqual(self.process(), "filtered")
        self.assertEqual(self.repo.mark.call_args.args[2], "skipped")
        self.telegram.send.assert_not_called()

    @patch("main.requests.post")
    def test_telegram_client_sanitizes_network_errors(self, post):
        post.side_effect = m.requests.ReadTimeout("secret-token-in-url")
        with self.assertRaises(m.DeliveryUncertain) as error:
            m.Telegram("secret-token", "123").send("hello")
        self.assertNotIn("secret-token", str(error.exception))
        self.assertEqual(post.call_count, 1)

    @patch("main.requests.post")
    def test_telegram_api_ok_and_message_id_required(self, post):
        post.return_value = SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {"message_id": 12}})
        self.assertEqual(m.Telegram("fake", "123").send("hello"), 12)
        post.return_value = SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {}})
        with self.assertRaises(m.DeliveryUncertain):
            m.Telegram("fake", "123").send("hello")


if __name__ == "__main__":
    unittest.main()
