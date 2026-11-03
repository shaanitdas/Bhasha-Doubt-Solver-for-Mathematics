import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from sarvamai import SarvamAI


ROOT = Path(__file__).parent
DB_PATH = Path("/tmp/learner.db") if os.getenv("VERCEL") else ROOT / "learner.db"
MODEL = "sarvam-105b-conversations"
logger = logging.getLogger(__name__)
HIGH_CONFIDENCE = {"sure"}
FIXED_TAGS = {
    "square_of_sum_error",
    "inverse_operation_error",
    "distributive_property_error",
    "chain_rule_error",
    "speed_velocity_confusion",
    "sign_error",
    "none",
}
TAG_ALIASES = {
    "distributive_over_addition_error": "square_of_sum_error",
    "distributive_property_omission": "square_of_sum_error",
    "distributive_property_misapplication": "square_of_sum_error",
    "distributive_property_error": "square_of_sum_error",
    "incorrect_binomial_expansion": "square_of_sum_error",
    "addition_error": "square_of_sum_error",
    "inverse_operation_mistake": "inverse_operation_error",
    "wrong_inverse_operation": "inverse_operation_error",
    "sign_mistake": "sign_error",
}
TAG_LABELS = {
    "square_of_sum_error": "squaring a sum",
    "inverse_operation_error": "inverse operations",
    "distributive_property_error": "distributing terms",
    "chain_rule_error": "the chain rule",
    "speed_velocity_confusion": "speed and velocity",
    "sign_error": "positive and negative signs",
}


def load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(misconceptions)").fetchall()
        }
        if not columns:
            connection.execute(
                """
                CREATE TABLE misconceptions (
                    tag TEXT PRIMARY KEY,
                    count INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    recurring INTEGER NOT NULL DEFAULT 0,
                    hint_level INTEGER NOT NULL DEFAULT 1
                )
                """
            )
        else:
            if "recurring" not in columns:
                connection.execute("ALTER TABLE misconceptions ADD COLUMN recurring INTEGER NOT NULL DEFAULT 0")
            if "hint_level" not in columns:
                connection.execute("ALTER TABLE misconceptions ADD COLUMN hint_level INTEGER NOT NULL DEFAULT 1")
        migrate_tags(connection)
        connection.commit()
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def canonical_tag(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    value = tag.strip().lower()
    value = re.sub(r"^recurring\s+", "", value)
    value = re.split(r",\s*|\s+\(", value, maxsplit=1)[0].strip()
    value = re.sub(r"[^a-z0-9_\- ]+", "", value)
    value = re.sub(r"\s+", "_", value)
    value = TAG_ALIASES.get(value, value)
    return "" if value in {"", "none", "unknown", "not_applicable"} else value if value in FIXED_TAGS else ""


def migrate_tags(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT tag, count, first_seen, last_seen, recurring, hint_level FROM misconceptions"
    ).fetchall()
    merged: dict[str, tuple[int, str, str, int]] = {}
    for row in rows:
        tag = canonical_tag(row["tag"])
        if not tag:
            continue
        count, first_seen, last_seen, hint_level = merged.get(
            tag, (0, row["first_seen"], row["last_seen"], 1)
        )
        merged[tag] = (
            count + row["count"],
            min(first_seen, row["first_seen"]),
            max(last_seen, row["last_seen"]),
            max(hint_level, row["hint_level"]),
        )
    connection.execute("DELETE FROM misconceptions")
    for tag, (count, first_seen, last_seen, hint_level) in merged.items():
        connection.execute(
            """
            INSERT INTO misconceptions(tag, count, first_seen, last_seen, recurring, hint_level)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tag, count, first_seen, last_seen, int(count >= 3), hint_level),
        )


def learner_model() -> list[dict[str, Any]]:
    with database() as connection:
        rows = connection.execute(
            "SELECT tag, count, recurring, hint_level, first_seen, last_seen FROM misconceptions "
            "ORDER BY count DESC, last_seen DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def clean_learner_model(items: Any) -> list[dict[str, Any]]:
    """Validate the browser-saved model before using it to personalize a reply."""
    if not isinstance(items, list):
        return []
    cleaned: dict[str, dict[str, Any]] = {}
    for item in items[:50]:
        if not isinstance(item, dict):
            continue
        tag = canonical_tag(str(item.get("tag", "")))
        if not tag:
            continue
        try:
            count = max(1, min(int(item.get("count", 1)), 1_000_000))
            hint_level = max(1, min(int(item.get("hint_level", 1)), 4))
        except (TypeError, ValueError):
            continue
        cleaned[tag] = {
            "tag": tag,
            "count": count,
            "recurring": count >= 3,
            "hint_level": hint_level,
            "first_seen": str(item.get("first_seen", ""))[:64],
            "last_seen": str(item.get("last_seen", ""))[:64],
        }
    return sorted(cleaned.values(), key=lambda item: (-item["count"], item["last_seen"]))


def update_learner_model(items: list[dict[str, Any]], tag: str) -> list[dict[str, Any]]:
    tag = canonical_tag(tag)
    if not tag:
        return items
    now = datetime.now(timezone.utc).isoformat()
    by_tag = {item["tag"]: item for item in items}
    if tag in by_tag:
        item = by_tag[tag]
        item["count"] += 1
        item["last_seen"] = now
        item["recurring"] = item["count"] >= 3
        item["hint_level"] = min(item["hint_level"] + 1, 4)
    else:
        by_tag[tag] = {
            "tag": tag,
            "count": 1,
            "recurring": False,
            "hint_level": 1,
            "first_seen": now,
            "last_seen": now,
        }
    return sorted(by_tag.values(), key=lambda item: (-item["count"], item["last_seen"]))


def record_misconception(tag: str) -> None:
    tag = canonical_tag(tag)
    if not tag:
        return
    now = datetime.now(timezone.utc).isoformat()
    with database() as connection:
        connection.execute(
            """
            INSERT INTO misconceptions(tag, count, first_seen, last_seen, recurring, hint_level)
            VALUES (?, 1, ?, ?, 0, 1)
            ON CONFLICT(tag) DO UPDATE SET
                count = count + 1,
                last_seen = excluded.last_seen,
                recurring = CASE WHEN count + 1 >= 3 THEN 1 ELSE 0 END,
                hint_level = MIN(hint_level + 1, 4)
            """,
            (tag[:120], now, now),
        )
        connection.commit()


def extract_json(text: str) -> dict[str, Any] | None:
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", text, flags=re.I)
    decoder = json.JSONDecoder()
    for index, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def chat_json(
    client: SarvamAI,
    prompt: str,
    max_tokens: int = 500,
    required_keys: set[str] | None = None,
) -> dict[str, Any]:
    last_content = ""
    for attempt in range(2):
        response = client.chat.completions(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            top_p=1,
            max_tokens=max_tokens,
            reasoning_effort=None,
        )
        last_content = str(response.choices[0].message.content or "").strip()
        parsed = extract_json(last_content)
        if parsed is not None and (not required_keys or required_keys <= parsed.keys()):
            return parsed
        logger.warning("Invalid Sarvam response format (attempt %d)", attempt + 1)
    raise RuntimeError("Sarvam returned an invalid tutor response. Please try again.")


def answers_match(expected: Any, student: Any) -> bool:
    expected_text = re.sub(r"\s+", "", str(expected).lower().strip())
    student_text = re.sub(r"\s+", "", str(student).lower().strip())
    if not expected_text or not student_text:
        return False
    try:
        expected_number = Decimal(expected_text.replace(",", ""))
        student_number = Decimal(student_text.replace(",", ""))
        return expected_number.is_finite() and student_number.is_finite() and expected_number == student_number
    except InvalidOperation:
        return expected_text == student_text


def solve(
    question: str,
    attempt: str,
    confidence: str,
    hint_level: int,
    language: str,
    show_me: bool = False,
    context: list[dict[str, str]] | None = None,
    saved_learner_model: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    api_key = os.environ.get("SARVAM_API_KEY")
    if not api_key:
        raise RuntimeError("SARVAM_API_KEY is not set. Add it to .env first.")

    history = learner_model() if saved_learner_model is None else clean_learner_model(saved_learner_model)
    known_tags = ", ".join(
        f"{TAG_LABELS.get(item['tag'], 'this concept')} ({item['count']}x)"
        for item in history[:8]
    ) or "No prior misconceptions yet."
    client = SarvamAI(api_subscription_key=api_key)
    conversation_context = context or []
    context_text = "\n".join(
        f"{turn.get('role', 'user').title()}: {turn.get('content', '')}"
        for turn in conversation_context[-12:]
    ) or "No earlier turns in this question."
    language_instruction = {
        "Bengali": "Write in Bengali script (বাংলা), keeping standard math terms in English when clearer.",
        "Hinglish": "Write in natural Hinglish, using Hindi sentence structure with English technical terms.",
    }.get(language, f"Write for a student who prefers {language}; keep technical terms in English when useful.")
    answer_request = show_me
    if answer_request:
        answer_prompt = f"""
The student asks for the answer to this valid problem: {question}
{language_instruction}
Earlier turns in this question:
{context_text}
Give the full solution and one similar twin problem. Be warm and concise.
Return ONLY valid JSON with a "reply" key.
"""
        parsed = chat_json(client, answer_prompt, max_tokens=500)
        answer = parsed.get("reply")
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("Sarvam returned an empty solution. Please try again.")
        parsed["reply"] = answer.strip()
        parsed.update({"misconception_tag": "", "hint_level": 4, "show_me": False,
                       "student_is_correct": False, "verified": True, "model": MODEL,
                       "learner_model": history})
        return parsed
    verification_prompt = f"""
You are an independent math/science checker. Analyze the problem and the student's attempt
without coaching them. Solve the problem yourself, compare the student's reasoning to your
solution, compare the final answers numerically (or by normalized symbolic equality), and
classify the input. Return ONLY valid JSON:
{{
  "is_valid_problem": true,
  "student_is_correct": false,
  "final_answer": "the independently solved final answer",
  "student_final_answer": "the final answer from the student's attempt",
  "misconception_tag": "one fixed tag or none",
  "verdict": "internal checker note naming the first wrong step, or confirming correctness",
  "verification": "one sentence explaining the comparison"
}}
Use "is_valid_problem": false for gibberish, "idk" without reasoning, or off-topic requests.
Choose misconception_tag only from this fixed list:
square_of_sum_error, inverse_operation_error, distributive_property_error,
chain_rule_error, speed_velocity_confusion, sign_error, none.
Use none when correct, invalid, or unsupported. Never guess a misconception.
For these tags, use the first matching definition and include the exact tag:
- inverse_operation_error: wrong operation or number when moving a term across "="
  (for example 11 - 3 computed as 14). Examples: x + 3 = 11, x = 14; 2x + 3 = 11, 2x = 14.
- sign_error: the sign of a term is flipped or dropped. Examples: -x = 4, x = 4;
  3 - x = 7, 3 + x = 7.
- square_of_sum_error: (a+b)^2 treated as a^2 + b^2. Examples: (x+2)^2 = x^2+4;
  (a+b)^2 = a^2+b^2.
Arithmetic slips while isolating a variable are inverse_operation_error, not sign_error.
Question: {question}
Student's attempt: {attempt}
Earlier turns in this question:
{context_text}
"""
    verification = chat_json(
        client,
        verification_prompt,
        max_tokens=300,
        required_keys={"is_valid_problem", "student_is_correct", "final_answer", "student_final_answer"},
    )
    valid_problem = verification.get("is_valid_problem") is True
    student_is_correct = bool(
        verification.get("student_is_correct") is True
        and answers_match(
            verification.get("final_answer"),
            verification.get("student_final_answer"),
        )
    )
    verified_tag = canonical_tag(str(verification.get("misconception_tag", "")))
    if not valid_problem:
        return {
            "reply": "I didn't catch that. Can you tell me the math problem and what you tried?",
            "misconception_tag": "",
            "hint_level": 1,
            "show_me": False,
            "learner_model": history,
            "model": MODEL,
        }

    tag_history = next((item for item in history if item["tag"] == verified_tag), None)
    tag_hint_level = int(tag_history["hint_level"]) if tag_history else 1
    try:
        requested_hint_level = max(1, min(int(hint_level), 4))
    except (TypeError, ValueError):
        requested_hint_level = 1
    current_hint_level = (
        1
        if student_is_correct or not verified_tag
        else max(tag_hint_level, requested_hint_level)
    )
    prompt = f"""
You are Bhasha, a patient Socratic tutor. Never provide the full solution to the original
problem in this Guide-me path, including at Hint 4. Use the independent checker’s verdict
to guide your response without revealing its diagnosis or final answer. Ask the student to
inspect or test their own step first. At Hint 3 or higher, use a worked similar example
while leaving the original problem for the student to solve.
Hint ladder: 1=ask a guiding question, 2=small nudge, 3=worked similar example,
4=another focused prompt about the student's next step. The separate Show-me action
provides the full solution.
{language_instruction}
Known learner history (use friendly labels only): {known_tags}
Question: {question}
Student's attempt: {attempt}
Earlier turns in this question:
{context_text}
Confidence before feedback: {confidence}
Show-me requested: {show_me}
Independent verification: {verification.get("verification", "")}
Internal verification verdict: {verification.get("verdict", verification.get("verification", "") )}
Independent checker says the student's reasoning is {"correct" if student_is_correct else "not correct"}.
{"Because the student is correct, begin exactly with 'Yes, that’s right', explain in one line why the final answer matches, then ask exactly one extension question. Never say 'double-check' or give a hint ladder." if student_is_correct else ""}
Verified misconception tag is "{verified_tag or "none"}".

Return ONLY valid JSON with these keys:
{{
  "reply": "short tutor message",
  "hint_level": {current_hint_level},
  "show_me": false,
  "visual_approach": true
}}
Do not copy the checker’s verdict, these instructions, or internal tag names into the reply.
Keep the entire reply in the selected language. Avoid third-person wording such as "the
student's answer". If the student sounded sure but is wrong, acknowledge that naturally
in the selected language without using a fixed template. Avoid accusatory language.
Set show_me true when the next hint could benefit from a similar worked example. Do not
provide the original problem's full solution in this response.
"""
    parsed = chat_json(client, prompt, required_keys={"reply"})
    tag = verified_tag if not student_is_correct else ""
    if tag and saved_learner_model is None:
        record_misconception(tag)
    raw_reply = parsed.get("reply")
    if not isinstance(raw_reply, str) or not raw_reply.strip():
        raise RuntimeError("Sarvam returned an empty tutor response. Please try again.")
    reply = raw_reply.strip()
    for internal_tag, friendly_label in TAG_LABELS.items():
        reply = reply.replace(internal_tag, friendly_label)
    if student_is_correct and reply.lower().startswith("yes, that’s right"):
        reply = "Yes, that's right" + reply[len("Yes, that’s right"):]
    elif student_is_correct and not reply.lower().startswith("yes, that's right"):
        reply = f"Yes, that's right. {reply}"
    parsed["reply"] = reply
    parsed["misconception_tag"] = tag
    parsed["hint_level"] = min(current_hint_level + 1, 4) if not student_is_correct else 1
    parsed["show_me"] = parsed.get("show_me") is True or current_hint_level >= 2
    if show_me:
        parsed["show_me"] = False
    parsed["student_is_correct"] = student_is_correct
    parsed["verified"] = True
    parsed["model"] = MODEL
    parsed["learner_model"] = (
        learner_model()
        if saved_learner_model is None
        else update_learner_model(history, tag)
    )
    return parsed


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/model":
            self.send_json({"misconceptions": learner_model()})
            return
        if path in {"/", "/index.html"}:
            body = (ROOT / "public" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/reset":
            with database() as connection:
                connection.execute("DELETE FROM misconceptions")
                connection.commit()
            self.send_json({"misconceptions": []})
            return
        if path != "/api/solve":
            self.send_error(404)
            return
        try:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json({"error": "Invalid Content-Length header."}, 400)
                return
            if length < 0 or length > 64 * 1024:
                self.send_json({"error": "Request is too large."}, 413)
                return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                self.send_json({"error": "Request body must be a JSON object."}, 400)
                return
            if not isinstance(payload.get("question"), str):
                self.send_json({"error": "Question must be text."}, 400)
                return
            question = payload["question"].strip()
            raw_attempt = payload.get("attempt", "")
            if not isinstance(raw_attempt, str):
                self.send_json({"error": "Attempt must be text."}, 400)
                return
            attempt = raw_attempt.strip()
            show_me = payload.get("show_me") is True
            if not question or (not attempt and not show_me):
                self.send_json(
                    {"error": "Tell me what you've tried, or use Show answer for the full solution."},
                    400,
                )
                return
            raw_context = payload.get("context", [])
            context = [
                {
                    "role": str(turn.get("role", "user"))[:16],
                    "content": str(turn.get("content", ""))[:4000],
                }
                for turn in raw_context[-12:]
                if isinstance(turn, dict)
            ] if isinstance(raw_context, list) else []
            result = solve(
                question,
                attempt,
                str(payload.get("confidence", "unsure")),
                payload.get("hint_level", 1),
                str(payload.get("language", "English")),
                show_me,
                context,
                payload.get("learner_model") if isinstance(payload.get("learner_model"), list) else None,
            )
            self.send_json(result)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            self.send_json({"error": "Request body must contain valid JSON."}, 400)
        except Exception:
            logger.exception("Failed to solve student request")
            self.send_json({"error": "I couldn't process that right now. Please try again."}, 500)

    def log_message(self, format: str, *args: Any) -> None:
        return


if __name__ == "__main__":
    load_env()
    with database():
        pass
    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    print("Bhasha Doubt Solver: http://127.0.0.1:8000")
    server.serve_forever()
