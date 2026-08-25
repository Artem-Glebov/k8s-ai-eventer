"""Plain SMTP client for Critical-transition target alerts. Deterministic
subject/body only - no LLM call here, consistent with this project's
facts-then-narrative philosophy: a notification is a fact delivery, not a
place to introduce model latency/hallucination risk."""

import logging
import smtplib
from email.mime.text import MIMEText

logger = logging.getLogger("notify")


def send_critical_alert(
    smtp_host: str, smtp_port: int, smtp_username: str, smtp_password: str, smtp_from: str,
    smtp_use_tls: bool, recipients: list[str], target_name: str, namespace: str,
    selector_kind: str, selector_name: str, findings: list[dict], remediation: str | None,
) -> bool:
    if not recipients:
        logger.info(
            "target=%s went Critical but no recipients have notifications enabled - skipping email",
            target_name,
        )
        return False

    subject = f"[ai-k8s-eventer] {target_name} is now Critical"
    lines = [
        f"Watch target '{target_name}' ({selector_kind} {selector_name} in namespace {namespace}) "
        f"transitioned to Critical.",
        "",
        "Top findings:",
        *[f"- {f['detail']}" for f in findings],
        "",
        f"Suggested fix: {remediation or 'none'}",
    ]
    msg = MIMEText("\n".join(lines))
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
            if smtp_use_tls:
                s.starttls()
            if smtp_username:
                s.login(smtp_username, smtp_password)
            s.sendmail(smtp_from, recipients, msg.as_string())
        logger.info("sent Critical alert for target=%s to %d recipient(s)", target_name, len(recipients))
        return True
    except Exception:
        # A failed send must not crash the analyzer tick - the caller leaves
        # notification_state alone on failure so the next tick retries.
        logger.exception("failed to send Critical alert for target=%s", target_name)
        return False
