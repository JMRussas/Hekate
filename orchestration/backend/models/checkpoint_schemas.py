#  Orchestration Engine - Predefined Checkpoint Schemas
#
#  JSON Schema definitions for structured checkpoint responses.
#  Used by the checkpoint system to validate user/agent responses.
#
#  Depends on: jsonschema
#  Used by:    routes/checkpoints.py, services/*

from jsonschema import validate, ValidationError

# ---------------------------------------------------------------------------
# Predefined Schemas
# ---------------------------------------------------------------------------

APPROVE_REJECT: dict = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["approve", "reject"],
        },
        "reason": {
            "type": "string",
            "maxLength": 10000,
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}

SELECT_OPTION: dict = {
    "type": "object",
    "properties": {
        "selected": {
            "type": "string",
            "minLength": 1,
        },
        "reason": {
            "type": "string",
            "maxLength": 10000,
        },
    },
    "required": ["selected"],
    "additionalProperties": False,
}

PROVIDE_FILE_PATH: dict = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "description": "Absolute file path",
        },
        "description": {
            "type": "string",
            "maxLength": 10000,
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}

FREE_TEXT_WITH_REASON: dict = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "minLength": 1,
        },
        "reason": {
            "type": "string",
            "minLength": 1,
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        },
    },
    "required": ["text", "reason", "confidence"],
    "additionalProperties": False,
}

PREDEFINED_SCHEMAS: dict[str, dict] = {
    "approve_reject": APPROVE_REJECT,
    "select_option": SELECT_OPTION,
    "provide_file_path": PROVIDE_FILE_PATH,
    "free_text_with_reason": FREE_TEXT_WITH_REASON,
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_checkpoint_response(schema: dict, data: dict) -> tuple[bool, str | None]:
    """Validate checkpoint response data against a JSON Schema.

    Returns (True, None) on success, (False, error_message) on failure.
    """
    try:
        validate(instance=data, schema=schema)
        return True, None
    except ValidationError as exc:
        return False, exc.message
