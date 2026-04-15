from runner.plan_validator import PlanValidationError, validate_plan, validate_run_command


def test_accepts_python_module_pytest():
    result = validate_run_command("python -m pytest tests/test_plan_validator.py")
    assert result.valid is True
    assert result.reasons == []


def test_rejects_bare_pytest():
    result = validate_run_command("pytest tests/test_plan_validator.py")
    assert result.valid is False
    assert any("bare pytest is forbidden" in reason for reason in result.reasons)


def test_rejects_placeholder_command():
    result = validate_run_command("command to generate a new plan")
    assert result.valid is False
    assert any("disallowed pattern matched" in reason for reason in result.reasons)


def test_rejects_verify_language():
    result = validate_run_command("verify planner functionality")
    assert result.valid is False


def test_validate_plan_raises_for_invalid_run_command():
    plan = {
        "steps": [
            {
                "title": "bad",
                "type": "run_command",
                "command": "test suite for planner",
            }
        ]
    }
    try:
        validate_plan(plan)
        assert False, "Expected PlanValidationError"
    except PlanValidationError as exc:
        assert "step 1 invalid command" in str(exc)


def test_validate_plan_accepts_valid_run_command():
    plan = {
        "steps": [
            {
                "title": "good",
                "type": "run_command",
                "command": "python -m pytest tests/test_plan_validator.py",
            }
        ]
    }
    validate_plan(plan)
