"""Minimal, dependency-free client for the Anthropic Messages API (streaming + tool use).

Kept deliberately small so it has no version conflicts with other apps on the bench.
"""

import json
import random
import time

import requests

API_VERSION = "2023-06-01"
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class ClaudeAPIError(Exception):
	def __init__(self, message, status=None, error_type=None):
		super().__init__(message)
		self.status = status
		self.error_type = error_type

	def friendly(self):
		if self.status == 401:
			return "The Claude API key is invalid. Please ask your System Manager to check Fibersoft AI Settings."
		if self.status == 403:
			return "The Claude API key does not have permission for this request."
		if self.status == 404:
			return "The configured Claude model was not found. Please check the Model in Fibersoft AI Settings."
		if self.status == 429:
			return "The Claude API rate limit was reached. Please wait a moment and try again."
		if self.status in (500, 502, 503, 504, 529):
			return "The Claude API is temporarily overloaded. Please try again in a minute."
		if self.status == 400 and "credit" in str(self).lower():
			return "The Anthropic account has run out of credit. Please top up the account."
		return f"Claude API error: {self}"


class StreamCancelled(Exception):
	pass


class ClaudeClient:
	def __init__(self, api_key, base_url="https://api.anthropic.com", timeout=120, max_retries=3, session=None):
		if not api_key:
			raise ClaudeAPIError("No Claude API key configured", status=401)
		self.api_key = api_key
		self.url = base_url.rstrip("/") + "/v1/messages"
		self.timeout = timeout
		self.max_retries = max_retries
		self.session = session or requests.Session()

	def _headers(self):
		return {
			"x-api-key": self.api_key,
			"anthropic-version": API_VERSION,
			"content-type": "application/json",
		}

	def create(self, payload, on_text=None, should_cancel=None):
		"""Send a Messages request. Streams when on_text is given. Returns the assembled message dict."""
		stream = on_text is not None
		body = dict(payload, stream=stream)
		attempt = 0
		while True:
			attempt += 1
			emitted = {"any": False}
			try:
				resp = self.session.post(
					self.url,
					headers=self._headers(),
					data=json.dumps(body, default=str),
					timeout=(15, self.timeout),
					stream=stream,
				)
				if resp.status_code != 200:
					self._raise_for_response(resp)
				if not stream:
					return resp.json()
				return self._consume_stream(resp, on_text, should_cancel, emitted)
			except StreamCancelled:
				raise
			except ClaudeAPIError as e:
				retryable = e.status in RETRY_STATUS or e.error_type == "overloaded_error"
				if not retryable or attempt > self.max_retries or emitted["any"]:
					raise
				self._sleep(attempt, getattr(e, "retry_after", None))
			except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
				if attempt > self.max_retries or emitted["any"]:
					raise ClaudeAPIError(f"Network error talking to Claude API: {e}", status=503) from e
				self._sleep(attempt)

	def _sleep(self, attempt, retry_after=None):
		delay = retry_after if retry_after else min(2 ** attempt, 20) + random.random()
		time.sleep(min(float(delay), 30))

	def _raise_for_response(self, resp):
		try:
			data = resp.json()
			err = data.get("error") or {}
			message, etype = err.get("message") or resp.text, err.get("type")
		except Exception:
			message, etype = resp.text[:500], None
		e = ClaudeAPIError(message, status=resp.status_code, error_type=etype)
		ra = resp.headers.get("retry-after")
		if ra:
			try:
				e.retry_after = float(ra)
			except ValueError:
				pass
		raise e

	def _consume_stream(self, resp, on_text, should_cancel, emitted):
		parser = StreamAssembler()
		event_name, data_lines = None, []
		for raw in resp.iter_lines(decode_unicode=True):
			if should_cancel and should_cancel():
				resp.close()
				raise StreamCancelled()
			if raw is None:
				continue
			line = raw.rstrip("\r")
			if line == "":
				if data_lines:
					text = parser.feed(event_name, "\n".join(data_lines))
					if text:
						emitted["any"] = True
						on_text(text)
				event_name, data_lines = None, []
				continue
			if line.startswith(":"):
				continue
			if line.startswith("event:"):
				event_name = line[6:].strip()
			elif line.startswith("data:"):
				data_lines.append(line[5:].lstrip())
		if data_lines:
			text = parser.feed(event_name, "\n".join(data_lines))
			if text:
				on_text(text)
		return parser.result()


class StreamAssembler:
	"""Rebuilds a complete Messages API response from server-sent events."""

	def __init__(self):
		self.message = {"content": [], "usage": {}, "stop_reason": None}
		self.blocks = {}
		self.partial_json = {}

	def feed(self, event_name, data):
		try:
			ev = json.loads(data)
		except ValueError:
			return None
		etype = ev.get("type") or event_name
		if etype == "message_start":
			msg = ev.get("message") or {}
			self.message.update({k: v for k, v in msg.items() if k not in ("content",)})
			self.message["usage"] = dict(msg.get("usage") or {})
		elif etype == "content_block_start":
			idx = ev["index"]
			block = dict(ev.get("content_block") or {})
			if block.get("type") == "tool_use":
				block["input"] = {}
				self.partial_json[idx] = ""
			if block.get("type") == "text":
				block["text"] = block.get("text") or ""
			self.blocks[idx] = block
		elif etype == "content_block_delta":
			idx = ev["index"]
			delta = ev.get("delta") or {}
			block = self.blocks.setdefault(idx, {"type": "text", "text": ""})
			if delta.get("type") == "text_delta":
				block["text"] = block.get("text", "") + delta.get("text", "")
				return delta.get("text", "")
			if delta.get("type") == "input_json_delta":
				self.partial_json[idx] = self.partial_json.get(idx, "") + delta.get("partial_json", "")
		elif etype == "content_block_stop":
			idx = ev["index"]
			if idx in self.partial_json:
				raw = self.partial_json.pop(idx)
				try:
					self.blocks[idx]["input"] = json.loads(raw) if raw.strip() else {}
				except ValueError:
					self.blocks[idx]["input"] = {"__invalid_json__": raw}
		elif etype == "message_delta":
			delta = ev.get("delta") or {}
			if "stop_reason" in delta:
				self.message["stop_reason"] = delta["stop_reason"]
			usage = ev.get("usage") or {}
			for k, v in usage.items():
				if v is not None:
					self.message["usage"][k] = v
		elif etype == "error":
			err = ev.get("error") or {}
			status = 529 if err.get("type") == "overloaded_error" else 500
			raise ClaudeAPIError(err.get("message") or "stream error", status=status, error_type=err.get("type"))
		return None

	def result(self):
		self.message["content"] = [self.blocks[i] for i in sorted(self.blocks)]
		return self.message
