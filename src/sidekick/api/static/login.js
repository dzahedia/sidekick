const loginForm = document.getElementById("login-form");
const registerForm = document.getElementById("register-form");
const unameInput = document.getElementById("uname");
const upassInput = document.getElementById("upass");
const loginError = document.getElementById("login-error");
const logoutBtn = document.getElementById("logout-btn");
const userNameEl = document.getElementById("user-name");

const regNameInput = document.getElementById("reg-name");
const regUnameInput = document.getElementById("reg-uname");
const regUpassInput = document.getElementById("reg-upass");
const regFolderInput = document.getElementById("reg-folder");
const registerError = document.getElementById("register-error");

function renderUserName() {
    // Show the currently logged-in user's name next to the Logout button,
    // if a session is still stored from a previous login.
    try {
        const raw = sessionStorage.getItem("sidekick_user");
        if (raw) {
            const user = JSON.parse(raw);
            if (user && user.name) {
                userNameEl.textContent = user.name;
                return;
            }
        }
    } catch {
        // Ignore malformed session data.
    }
    userNameEl.textContent = "";
}

function showRegisterError(message) {
    registerError.textContent = message;
    registerError.hidden = false;
}

function clearRegisterError() {
    registerError.textContent = "";
    registerError.hidden = true;
}

function showLoginError(message) {
    loginError.textContent = message;
    loginError.hidden = false;
}

function clearLoginError() {
    loginError.textContent = "";
    loginError.hidden = true;
    loginError.classList.remove("success");
}

async function handleLogin(event) {
    event.preventDefault();
    clearLoginError();

    try {
        const res = await fetch("/api/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ uname: unameInput.value, upass: upassInput.value }),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(body.detail || `Login failed (${res.status})`);
        }
        // Store the session so the main app can use it, then go to the app.
        sessionStorage.setItem("sidekick_user", JSON.stringify(body));
        window.location.href = "/app";
    } catch (err) {
        showLoginError(err.message);
    }
}

async function handleLogout() {
    // Read the stored session so we can send the token to the server and
    // clear every piece of user data we persisted locally.
    let user = null;
    try {
        const raw = sessionStorage.getItem("sidekick_user");
        if (raw) user = JSON.parse(raw);
    } catch {
        user = null;
    }

    try {
        const headers = {};
        if (user && user.token) {
            headers["Authorization"] = `Bearer ${user.token}`;
        }
        await fetch("/api/logout", { method: "POST", headers });
    } catch {
        // Best effort: clearing the local session is what matters.
    }

    // Clear all locally stored user data (session + local storage).
    sessionStorage.removeItem("sidekick_user");
    localStorage.removeItem("sidekick_user");
    userNameEl.textContent = "";
    window.location.href = "/login";
}

async function handleRegister(event) {
    event.preventDefault();
    clearRegisterError();

    try {
        const res = await fetch("/api/register", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: regNameInput.value,
                uname: regUnameInput.value,
                upass: regUpassInput.value,
                folder: regFolderInput.value,
            }),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(body.detail || `Registration failed (${res.status})`);
        }
        // Pre-fill the login form so the user can sign in right away.
        // Clear any stale session from a previous user so the new user's
        // login starts from a clean slate.
        sessionStorage.removeItem("sidekick_user");
        unameInput.value = body.uname;
        upassInput.value = regUpassInput.value;
        loginError.textContent = "Registered! You can now log in.";
        loginError.hidden = false;
        loginError.classList.add("success");
    } catch (err) {
        showRegisterError(err.message);
    }
}

loginForm.addEventListener("submit", handleLogin);
registerForm.addEventListener("submit", handleRegister);
logoutBtn.addEventListener("click", handleLogout);

// Show the logged-in user's name (if any) next to the Logout button.
renderUserName();
