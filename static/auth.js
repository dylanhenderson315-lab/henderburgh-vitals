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

/**
 * Full auth picture for Guest Mode:
 * - unlocked: real admin password session
 * - guest: house-wide light control open (no password), auto-expiring
 * - guest_expires: ISO timestamp when guest access auto-locks
 *
 * Light control UI should treat (unlocked || guest) as "can control lights".
 * Admin-only panels (guest toggle, access requests, model backups) need unlocked.
 */
async function getAuthStatus() {
  try {
    const res = await fetch('/api/auth/status', { credentials: 'same-origin' });
    if (!res.ok) return { unlocked: false, guest: false, guest_expires: null, configured: false };
    const data = await res.json();
    const unlocked = !!data.unlocked;
    const guest = !!data.guest;
    return {
      unlocked,
      guest,
      guest_expires: data.guest_expires || null,
      configured: !!data.configured,
      can_control: (data.can_control != null) ? !!data.can_control : (unlocked || guest),
    };
  } catch {
    return { unlocked: false, guest: false, guest_expires: null, configured: false, can_control: false };
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
