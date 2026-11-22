import jwt

from app.main import settings

from conftest import login

INTERNAL = {"X-Internal-Token": "token-internal-uji"}


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_demo_accounts_have_stable_ids_and_roles(client):
    for email, uid, role in (("pendonor@demo.test", "2", "donor"), ("pemohon@demo.test", "3", "requester"),
                             ("petugas@demo.test", "4", "staff")):
        user = login(client, email, "demo-uji-123")["user"]
        assert (user["id"], user["role"]) == (uid, role)


def test_register_login_me_and_jwt_claims(client):
    r = client.post("/register", json={"email": "Budi@Uji.test", "password": "rahasia-123", "name": "Budi Uji"})
    assert r.status_code == 201 and r.json()["data"]["role"] == "donor" and r.json()["data"]["email"] == "budi@uji.test"
    assert client.post("/register", json={"email": "budi@uji.test", "password": "rahasia-123",
                                          "name": "Budi"}).json()["error"]["code"] == "conflict"
    tokens = login(client, "budi@uji.test", "rahasia-123")
    claims = jwt.decode(tokens["access_token"], settings.jwt_secret, algorithms=["HS256"])
    assert claims["role"] == "donor" and claims["sub"] == tokens["user"]["id"] and claims["name"] == "Budi Uji"
    me = client.get("/me", headers={"Authorization": "Bearer " + tokens["access_token"]})
    assert me.status_code == 200 and me.json()["data"]["email"] == "budi@uji.test"
    assert client.get("/me").json()["error"]["code"] == "invalid_token"


def test_register_validation_and_no_self_staff(client):
    assert client.post("/register", json={"email": "x@uji.test", "password": "pendek", "name": "X"}).status_code == 422
    assert client.post("/register", json={"email": "bukan-email", "password": "rahasia-123",
                                          "name": "Xy"}).status_code == 422
    r = client.post("/register", json={"email": "s@uji.test", "password": "rahasia-123", "name": "Sy", "role": "staff"})
    assert r.status_code == 422


def test_wrong_password(client):
    r = client.post("/login", json={"email": "pendonor@demo.test", "password": "salah"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"


def test_refresh_rotation_and_logout(client):
    t = login(client, "pemohon@demo.test", "demo-uji-123")
    r = client.post("/refresh", json={"refresh_token": t["refresh_token"]})
    assert r.status_code == 200
    new = r.json()["data"]
    assert client.post("/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401  # sudah dirotasi
    assert client.post("/logout", json={"refresh_token": new["refresh_token"]}).status_code == 200
    assert client.post("/refresh", json={"refresh_token": new["refresh_token"]}).status_code == 401


def test_admin_creates_staff_and_non_admin_forbidden(client, admin_headers):
    r = client.post("/users", headers=admin_headers, json={"email": "petugas2@uji.test", "password": "rahasia-123",
                                                          "name": "Petugas Dua", "role": "staff"})
    assert r.status_code == 201 and r.json()["data"]["role"] == "staff"
    donor = {"Authorization": "Bearer " + login(client, "pendonor@demo.test", "demo-uji-123")["access_token"]}
    assert client.get("/users", headers=donor).json()["error"]["code"] == "forbidden"


def test_api_key_lifecycle(client, admin_headers):
    r = client.post("/api-keys", headers=admin_headers, json={"name": "KotaSiaga (Smart City)"})
    assert r.status_code == 201
    created = r.json()["data"]
    key = created["key"]
    assert key.startswith("dk_") and created["prefix"] == key[:7]
    assert "key" not in client.get("/api-keys", headers=admin_headers).json()["data"][-1]

    ok = client.post("/internal/verify-key", headers=INTERNAL, json={"key": key})
    assert ok.status_code == 200 and ok.json()["name"] == "KotaSiaga (Smart City)"
    assert client.post("/internal/verify-key", headers=INTERNAL, json={"key": "dk_salah"}).status_code == 401
    assert client.post("/internal/verify-key", json={"key": key}).status_code == 403  # tanpa token internal

    assert client.delete(f"/api-keys/{created['id']}", headers=admin_headers).json()["data"]["revoked_at"]
    assert client.post("/internal/verify-key", headers=INTERNAL, json={"key": key}).status_code == 401


def test_seed_api_key_is_valid(client):
    assert client.post("/internal/verify-key", headers=INTERNAL, json={"key": "dk_kunci_uji_seed"}).status_code == 200


def test_seq_rises_on_change(client, admin_headers):
    before = client.get("/users", headers=admin_headers).json()["meta"]["seq"]
    client.post("/api-keys", headers=admin_headers, json={"name": "JelajahLokal (Tourism)"})
    assert client.get("/users", headers=admin_headers).json()["meta"]["seq"] > before
