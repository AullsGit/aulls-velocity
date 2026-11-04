"""Erros com mensagem para o utilizador. O detalhe técnico fica só nos logs / painel admin."""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


GENERIC = "Something went wrong while creating your video. Your credits were refunded."

# código devolvido pelo worker -> mensagem para o utilizador (o frontend traduz pelo código)
JOB_ERRORS = {
    "reference_not_velocity": "This reference doesn't have clear cuts or effects for the AI to follow. "
                              "Try a different reference, or generate anyway. Your credits were refunded.",
    "reference_too_long": "Your reference video is too long. Trim it and try again. Your credits were refunded.",
    "source_too_long": "One of your videos is too long. Trim it and try again. Your credits were refunded.",
    "sources_total_too_long": "Your videos are too long in total. Remove some and try again. Your credits were refunded.",
    "invalid_media": "One of your files couldn't be read as a video or image. Your credits were refunded.",
    "no_sources": "Add at least one of your own videos. Your credits were refunded.",
    "file_too_large": "One of your files is too large. Your credits were refunded.",
    "missing_source": "The original clips for this project have expired, so it can't be re-exported.",
    "invalid_plan": "This edit can't be rendered as it is. Undo the last change and try again.",
    "interrupted": "The server restarted while creating your video. Your credits were refunded — please try again.",
    "internal": GENERIC,
    "timeout": GENERIC,
}


def job_error_message(code: str | None) -> str:
    return JOB_ERRORS.get(code or "internal", GENERIC)
