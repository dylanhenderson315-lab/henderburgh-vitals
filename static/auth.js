/** Shared admin auth helpers — session cookie, no tokens in page source. */

async function checkAdminUnlocked() {
  try {
    const res = await fetch('/api/auth/status', { credentials: 'same-origin' });
    if (!res.ok) return false;
    const data = await res.json();
    return !!data.unlocked;
  } catch {
    return false;
  }
}

/** Full auth picture: {unlocked (admin), guest (open-house light control), guest_expires}. */
async function getAuthStatus() {
  try {
    const res = await fetch('/api/auth/status', { credentials: 'same-origin' });
    if (!res.ok) return { unlocked: false, guest: false, guest_expires: null };
    return await res.json();
  } catch {
    return { unlocked: false, guest: false, guest_expires: null };
  }
}

async function unlockAdmin(token) {
  const res = await fetch('/api/auth/unlock', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || 'Invalid admin token');
  }
  return true;
}

async function logoutAdmin() {
  await fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' });
}

function adminFetch(url, options = {}) {
  return fetch(url, { ...options, credentials: 'same-origin' });
}
