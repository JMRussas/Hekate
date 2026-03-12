#  Orchestration Engine - API Key Tests
#
#  Tests for API key creation, validation, listing, and revocation.
#
#  Depends on: conftest.py (tmp_db, auth_service)
#  Used by:    CI



class TestApiKeyCreation:
    async def test_create_api_key_returns_full_key(self, auth_service, tmp_db):
        user = await auth_service.register("test@example.com", "testpass123")
        result = await auth_service.create_api_key(user["id"], "My Key")

        assert result["key"].startswith("orch_")
        assert len(result["key"]) > 20
        assert result["key_prefix"] == result["key"][:12]
        assert result["name"] == "My Key"
        assert result["id"]
        assert result["created_at"] > 0

    async def test_create_multiple_keys(self, auth_service, tmp_db):
        user = await auth_service.register("test@example.com", "testpass123")
        k1 = await auth_service.create_api_key(user["id"], "Key 1")
        k2 = await auth_service.create_api_key(user["id"], "Key 2")

        assert k1["key"] != k2["key"]
        assert k1["id"] != k2["id"]


class TestApiKeyValidation:
    async def test_valid_key_returns_user(self, auth_service, tmp_db):
        user = await auth_service.register("test@example.com", "testpass123")
        result = await auth_service.create_api_key(user["id"], "Test")

        validated = await auth_service.validate_api_key(result["key"])
        assert validated is not None
        assert validated["id"] == user["id"]
        assert validated["email"] == "test@example.com"

    async def test_invalid_key_returns_none(self, auth_service, tmp_db):
        validated = await auth_service.validate_api_key("orch_nonexistent")
        assert validated is None

    async def test_revoked_key_returns_none(self, auth_service, tmp_db):
        user = await auth_service.register("test@example.com", "testpass123")
        result = await auth_service.create_api_key(user["id"], "Test")

        await auth_service.revoke_api_key(result["id"], user["id"])
        validated = await auth_service.validate_api_key(result["key"])
        assert validated is None

    async def test_inactive_user_key_returns_none(self, auth_service, tmp_db):
        user = await auth_service.register("test@example.com", "testpass123")
        result = await auth_service.create_api_key(user["id"], "Test")

        # Deactivate user
        await tmp_db.execute_write(
            "UPDATE users SET is_active = 0 WHERE id = ?", (user["id"],)
        )
        validated = await auth_service.validate_api_key(result["key"])
        assert validated is None

    async def test_validates_updates_last_used(self, auth_service, tmp_db):
        user = await auth_service.register("test@example.com", "testpass123")
        result = await auth_service.create_api_key(user["id"], "Test")

        # Before validation, last_used_at should be null
        row = await tmp_db.fetchone(
            "SELECT last_used_at FROM api_keys WHERE id = ?", (result["id"],)
        )
        assert row["last_used_at"] is None

        await auth_service.validate_api_key(result["key"])

        row = await tmp_db.fetchone(
            "SELECT last_used_at FROM api_keys WHERE id = ?", (result["id"],)
        )
        assert row["last_used_at"] is not None


class TestApiKeyRevocation:
    async def test_revoke_returns_true(self, auth_service, tmp_db):
        user = await auth_service.register("test@example.com", "testpass123")
        result = await auth_service.create_api_key(user["id"], "Test")

        assert await auth_service.revoke_api_key(result["id"], user["id"]) is True

    async def test_revoke_wrong_user_returns_false(self, auth_service, tmp_db):
        user = await auth_service.register("test@example.com", "testpass123")
        result = await auth_service.create_api_key(user["id"], "Test")

        assert await auth_service.revoke_api_key(result["id"], "other_user") is False

    async def test_revoke_nonexistent_returns_false(self, auth_service, tmp_db):
        user = await auth_service.register("test@example.com", "testpass123")
        assert await auth_service.revoke_api_key("nonexistent", user["id"]) is False


class TestApiKeyListing:
    async def test_list_keys(self, auth_service, tmp_db):
        user = await auth_service.register("test@example.com", "testpass123")
        await auth_service.create_api_key(user["id"], "Key 1")
        await auth_service.create_api_key(user["id"], "Key 2")

        keys = await auth_service.list_api_keys(user["id"])
        assert len(keys) == 2
        # Keys should not contain hashes
        for k in keys:
            assert "key_hash" not in k
            assert "key" not in k
            assert "key_prefix" in k
            assert "name" in k

    async def test_list_keys_empty(self, auth_service, tmp_db):
        user = await auth_service.register("test@example.com", "testpass123")
        keys = await auth_service.list_api_keys(user["id"])
        assert keys == []
