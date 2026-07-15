"""NeuralTape v3 — classifier (D1.1).

Replaces v22/classifier.py. Key improvements:
- Extended prompt that asks for layer (working/episodic/semantic) + confidence.
- Output validated against ClassifierInsight dataclass.
- Integrates Redactor (Fase 0) before LLM call.
- Integrates CostPolicy (Fase 0) for budget checks.
- Persists classified insights directly to Storage as episodes.

Coexists with v2.2: v3 writes to Storage (SQLite) while v2.2 continues writing
to _Lex/memory.md. v2.2 is disabled only after validation (M1+M2 metrics).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# Allow both relative and direct imports (for tests).
_MODULE_DIR = Path(__file__).resolve().parent
if str(_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_MODULE_DIR))

from config import V3Config
from cost import CostPolicy
from project import Project
from redaction import Redactor, RedactionEvent
from storage import Episode, Storage

log = logging.getLogger("neural-tape-v3")

# Max characters for the transcript sent to LLM (matches v2.2).
MAX_TRANSCRIPT_CHARS = 30000

# The prompt template. {redacted_summary} is filled with the redaction summary
# (or empty if clean). {transcript} is the actual content.
CLASSIFIER_PROMPT = """Sei Lex, l'agente AI senior developer di Guglielmo. Hai appena concluso una sessione di lavoro in VS Code.

{redacted_summary}

Il tuo compito: estrarre insight strutturati per il sistema di memoria a layer di NeuralTape v3.

Categorie:
- "pattern": flussi di lavoro ricorrenti di Guglielmo, abitudini, modi di operare
- "decision": decisioni architetturali o strategiche (perché si è scelto X e non Y)
- "anti-pattern": cose che falliscono, vengono respinte, o si rivelano errori
- "preference": preferenze di Guglielmo (linguaggio, tool, approccio)
- "tool": quirk di API/framework/librerie scoperti (vincoli reali, signature corrette, limiti)
- "warning": errori critici, insidie, situazioni da evitare

Layer (vitalità):
- "working": roba utile ORA, riferimenti immediati, dettagli di sessione. Vita: minuti-ore.
- "episodic": eventi importanti, bug fix non banali, scoperte API. Vita: settimane.
- "semantic": pattern ricorrenti, preferenze stabili, decisioni architetturali con rationale. Vita: mesi-permanente.

Per ogni insight degno, restituisci:
- "category": una delle 6 sopra
- "title": titolo breve 5-12 parole
- "context": 1 riga — quando/perché è emerso
- "implication": 1 riga — cosa cambia per le future raccomandazioni
- "layer": "working" | "episodic" | "semantic"
- "confidence": float 0.0-1.0 (quanto sei sicuro che questo insight sia vero e utile)

REGOLE:
- Massimo 8 insight (meglio pochi e giusti).
- Se la sessione è routine senza apprendimenti reali, restituisci {{"insights": []}}.
- Non inventare: basati SOLO sul contenuto della trascrizione.
- Usa "preference" solo quando compare una preferenza esplicita dell'utente;
    una scelta implementativa o architetturale e' una "decision".
- Usa "semantic" solo se la trascrizione dimostra ricorrenza o stabilita';
    un evento o una decisione osservati una sola volta sono "episodic".
- Per failure ed eccezioni conserva il nome esatto dell'errore osservato e non reinterpretarlo.
- Rispondi SOLO con JSON valido, nessun markdown, nessun commento.

Formato output:
{{"insights": [{{"category":"...","title":"...","context":"...","implication":"...","layer":"...","confidence":0.0}}]}}

Trascrizione:
---
{transcript}
---"""


class ClassificationError(RuntimeError):
    """The classifier could not complete a trustworthy classification."""


class ClassificationDeferred(ClassificationError):
    """Classification was intentionally deferred by the cost policy."""


@dataclass
class ClassifierInsight:
    category: str
    title: str
    context: str
    implication: str
    layer: str          # "working" | "episodic" | "semantic"
    confidence: float

    @classmethod
    def from_dict(cls, d: dict) -> ClassifierInsight:
        return cls(
            category=str(d.get("category", "")),
            title=str(d.get("title", "")),
            context=str(d.get("context", "")),
            implication=str(d.get("implication", "")),
            layer=str(d.get("layer", "working")),
            confidence=float(d.get("confidence", 0.0)),
        )

    def validate(self) -> list[str]:
        errors = []
        if self.category not in {"pattern", "decision", "anti-pattern", "preference", "tool", "warning"}:
            errors.append(f"invalid category: {self.category!r}")
        if not self.title:
            errors.append("title is empty")
        if self.layer not in {"working", "episodic", "semantic"}:
            errors.append(f"invalid layer: {self.layer!r}")
        if not (0.0 <= self.confidence <= 1.0):
            errors.append(f"confidence out of range: {self.confidence}")
        return errors


class ClassifierV3:
    """Classify a transcript into layered insights via OpenAI-compatible LLM.

    Integrates:
    - Redactor (Fase 0) — redacts secrets before LLM
    - CostPolicy (Fase 0) — respects daily budget
    - Storage (Fase 0) — persists classified episodes
    """

    def __init__(
        self,
        config: V3Config,
        project: Project,
        storage: Storage,
        redactor: Redactor,
        cost_policy: CostPolicy,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ):
        self.config = config
        self.project = project
        self.storage = storage
        self.redactor = redactor
        self.cost_policy = cost_policy

        # LLM endpoint config (static .env loading, like v2.2)
        if api_key:
            self.api_key = api_key
        else:
            self.api_key = (
                os.environ.get("LLM_API_KEY")
                or os.environ.get("DEEPSEEK_API_KEY")
                or ""
            )
        if not self.api_key:
            raise RuntimeError(
                "No LLM_API_KEY. Set in .env (LLM_API_KEY=...) or pass directly."
            )
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL", "deepseek-v4-flash")

    # ---- public API -----------------------------------------------------

    def classify(self, transcript_text: str, session_id: str) -> list[ClassifierInsight]:
        """Redact → check budget → LLM → parse → validate. Returns valid insights.

        This does NOT write to storage. Use classify_and_persist for that.
        """
        # 1. Redact
        redacted, redaction_events = self.redactor.redact(transcript_text)
        redaction_summary = self.redactor.summary(redaction_events)
        if redaction_events:
            log.info("redaction: %s", redaction_summary)

        # 2. Check budget BEFORE LLM call
        allowed, reason = self.cost_policy.can_call()
        if not allowed:
            log.warning("LLM call skipped: %s", reason)
            raise ClassificationDeferred(f"LLM call skipped: {reason}")

        # 3. Classify
        chunks = list(reversed(self._split(redacted)))
        valid: list[ClassifierInsight] = []
        seen_titles: set[str] = set()

        for i, chunk in enumerate(chunks):
            if i > 0:
                log.info("classifying chunk %d/%d (newest first)", i + 1, len(chunks))

            prompt = CLASSIFIER_PROMPT.format(
                transcript=chunk,
                redacted_summary=self._redaction_comment(redaction_summary),
            )

            try:
                response = self._chat_completion(prompt)
                tokens_used = self._estimate_tokens(prompt, response)
                self.cost_policy.record_call(tokens_used)
            except Exception as error:
                log.error("LLM call failed: %s", error, exc_info=True)
                raise ClassificationError(f"LLM call failed: {error}") from error

            content = response.get("choices", [{}])[0].get("message", {}).get("content") or ""
            try:
                data = json.loads(content)
            except json.JSONDecodeError as error:
                log.warning("LLM returned non-JSON. First 300 chars: %s", content[:300])
                raise ClassificationError("LLM returned non-JSON") from error

            insights = data.get("insights", [])
            if not isinstance(insights, list):
                log.warning("LLM 'insights' is not a list")
                raise ClassificationError("LLM 'insights' is not a list")

            for d in insights:
                if not isinstance(d, dict):
                    continue
                try:
                    ins = ClassifierInsight.from_dict(d)
                except (ValueError, TypeError) as e:
                    log.warning("skipping malformed insight: %s", e)
                    continue

                errs = ins.validate()
                if errs:
                    log.warning("skipping invalid insight: %s", "; ".join(errs))
                    continue

                # Dedup by normalized title
                norm = " ".join(ins.title.lower().split())
                if norm in seen_titles:
                    continue
                seen_titles.add(norm)

                valid.append(ins)
                if len(valid) >= 8:
                    return valid

        return valid

    def classify_and_persist(self, transcript_text: str, session_id: str,
                             project_id: str) -> int:
        """Classify and immediately write insights to Storage as episodes.

        Returns: number of episodes written.
        """
        insights = self.classify(transcript_text, session_id)
        if not insights:
            return 0

        written = 0
        for ins in insights:
            ep = Episode(
                project_id=project_id,
                kind=ins.layer,
                source_type="transcript",
                source_ref=session_id,
                category=ins.category,
                title=ins.title,
                body=f"{ins.context}\n\n{ins.implication}",
                confidence=ins.confidence,
            )
            self.storage.put_episode(ep)
            written += 1
            log.debug("episode written: %s [%s] (conf=%.2f)", ins.title, ins.layer, ins.confidence)

        log.info("classified & persisted %d episode(s) from session %s", written, session_id)
        return written

    # ---- internals ------------------------------------------------------

    def _split(self, text: str, max_chars: int = MAX_TRANSCRIPT_CHARS) -> list[str]:
        if len(text) <= max_chars:
            return [text]
        chunks: list[str] = []
        current: list[str] = []
        cur_size = 0
        for line in text.splitlines(keepends=True):
            if current and cur_size + len(line) > max_chars:
                chunks.append("".join(current))
                current = []
                cur_size = 0
            current.append(line)
            cur_size += len(line)
        if current:
            chunks.append("".join(current))
        return chunks

    def _redaction_comment(self, summary: str) -> str:
        if summary.startswith("redaction: clean"):
            return ""
        return f"(Nota: nel transcript qui sotto alcuni secret sono stati sostituiti con [REDACTED:...]. Ignora le sostituzioni, sono solo artefatti di sicurezza. Riepilogo: {summary})"

    @staticmethod
    def _estimate_tokens(prompt: str, response: dict) -> int:
        """Rough token estimation: 1 token ≈ 4 chars. Used for cost tracking."""
        usage = response.get("usage", {})
        if isinstance(usage, dict) and usage.get("total_tokens"):
            return int(usage["total_tokens"])
        # Fallback: estimate from prompt + response content length.
        content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        return max(1, (len(prompt) + len(content)) // 4)

    def _chat_completion(self, prompt: str) -> dict:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        }
        url = f"{self.base_url}/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {body[:500]}") from e
