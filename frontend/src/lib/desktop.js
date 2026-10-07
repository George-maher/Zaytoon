/**
 * Native bridge wrapper for the Zaytoon desktop host.
 *
 * When the React app runs inside the desktop WebView, `window.pywebview.api`
 * provides access to native OS capabilities. This module wraps those calls in a
 * safe, consistent interface that degrades gracefully when the bridge is not
 * available (e.g., in a regular browser).
 *
 * Usage:
 *   import { desktop } from './lib/desktop';
 *
 *   if (desktop.available) {
 *     desktop.minimize();
 *   }
 *
 *   desktop.notify('Title', 'Message'); // no-op in browser
 */

let bridge = null;

if (typeof window !== 'undefined' && window.pywebview && window.pywebview.api) {
  bridge = window.pywebview.api;
}

/**
 * Call a bridge method and return a default value if the bridge is unavailable.
 * @param {string} method - The bridge method name.
 * @param {*} defaultValue - Value to return when the bridge is unavailable.
 * @param  {...any} args - Arguments to pass to the bridge method.
 * @returns {Promise<*>} The bridge result or defaultValue.
 */
function call(method, defaultValue, ...args) {
  if (bridge && typeof bridge[method] === 'function') {
    try {
      return Promise.resolve(bridge[method](...args));
    } catch {
      return Promise.resolve(defaultValue);
    }
  }
  return Promise.resolve(defaultValue);
}

export const desktop = {
  /** True when running inside the desktop host with a native bridge. */
  get available() {
    return bridge !== null;
  },

  // --- Window controls ---

  /** Minimize the window. */
  minimize() {
    return call('minimize', null);
  },

  /** Maximize the window. */
  maximize() {
    return call('maximize', null);
  },

  /** Restore the window from minimized or maximized state. */
  restore() {
    return call('restore', null);
  },

  /** Close the window and exit the application. */
  close() {
    return call('close', null);
  },

  /** Check if the window is maximized. */
  isMaximized() {
    return call('is_maximized', false);
  },

  /** Set whether the window stays on top. */
  setAlwaysOnTop(onTop) {
    return call('set_always_on_top', null, onTop);
  },

  /** Get the current window geometry and state. */
  getWindowState() {
    return call('get_window_state', {});
  },

  // --- File system ---

  /** Open a native folder picker. Returns the selected path or null. */
  openFolderDialog() {
    return call('open_folder_dialog', null);
  },

  /** Open a native file picker. Returns the selected path or null. */
  openFileDialog(fileTypes = '') {
    return call('open_file_dialog', null, fileTypes);
  },

  /** Reveal a file or folder in the OS file manager. */
  revealInOS(path) {
    return call('reveal_in_os', false, path);
  },

  /** Open a file or folder with the default OS application. */
  openPath(path) {
    return call('open_path', false, path);
  },

  // --- System ---

  /** Get system information. */
  getSystemInfo() {
    return call('get_system_info', {});
  },

  /** Get the application root directory. */
  getAppPath() {
    return call('get_app_path', '');
  },

  /** Get the backend WebSocket URL. */
  getBackendUrl() {
    return call('get_backend_url', '');
  },

  // --- Notifications ---

  /** Show a native OS notification. */
  notify(title, message) {
    return call('notify', false, title, message);
  },

  // --- External URLs ---

  /** Open a URL in the default browser. */
  openURL(url) {
    return call('open_url', false, url);
  },

  // --- Backend management ---

  /** Get the backend status. */
  getBackendStatus() {
    return call('get_backend_status', {});
  },

  /** Restart the backend process. */
  restartBackend() {
    return call('restart_backend', {});
  },

  // --- Clipboard ---

  /** Get the clipboard text. */
  getClipboard() {
    return call('get_clipboard', '');
  },

  /** Set the clipboard text. */
  setClipboard(text) {
    return call('set_clipboard', false, text);
  },
};

export default desktop;
