/*
 * Minimal browser runtime replacing the parts of Frappe's web bundle that the
 * My Sales pages use: frappe.call, frappe.ready, frappe.msgprint,
 * frappe.show_alert, frappe.ui.FileUploader and the __() translator.
 */
(function () {
	const boot = JSON.parse(document.getElementById("app-boot")?.textContent || "{}");
	const translations = boot.translations || {};

	window.__ = function (text, replace) {
		let message = translations[text] ?? text;
		if (Array.isArray(replace)) replace.forEach((value, index) => { message = message.replaceAll(`{${index}}`, value); });
		return message;
	};

	const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

	function serverMessages(data) {
		try {
			return JSON.parse(data._server_messages || "[]").map(item => {
				const parsed = typeof item === "string" ? JSON.parse(item) : item;
				return parsed.message || "";
			}).filter(Boolean);
		} catch (error) {
			return [];
		}
	}

	let freezeCount = 0;
	function freeze(message) {
		freezeCount += 1;
		let overlay = document.getElementById("app-freeze");
		if (!overlay) {
			overlay = document.createElement("div");
			overlay.id = "app-freeze";
			overlay.className = "app-freeze";
			overlay.innerHTML = '<div class="app-freeze-box"><span class="app-spinner"></span><span class="app-freeze-text"></span></div>';
			document.body.appendChild(overlay);
		}
		overlay.querySelector(".app-freeze-text").textContent = message || "Please wait…";
		overlay.hidden = false;
	}
	function unfreeze() {
		freezeCount = Math.max(0, freezeCount - 1);
		const overlay = document.getElementById("app-freeze");
		if (overlay && !freezeCount) overlay.hidden = true;
	}

	function msgprint(message, title) {
		if (message && typeof message === "object") {
			title = message.title || title;
			message = message.message;
		}
		let dialog = document.getElementById("app-msgprint");
		if (!dialog) {
			dialog = document.createElement("dialog");
			dialog.id = "app-msgprint";
			dialog.className = "app-msgprint";
			dialog.innerHTML = '<form method="dialog"><h3></h3><div class="app-msgprint-body"></div><button type="submit">OK</button></form>';
			document.body.appendChild(dialog);
		}
		dialog.querySelector("h3").textContent = title || __("Message");
		// Server messages are plain text; render them safely with line breaks.
		dialog.querySelector(".app-msgprint-body").innerHTML = escapeHtml(message).replace(/\n/g, "<br>");
		if (!dialog.open) dialog.showModal ? dialog.showModal() : dialog.setAttribute("open", "");
	}

	function show_alert(options, seconds) {
		if (typeof options === "string") options = { message: options };
		let stack = document.getElementById("app-alerts");
		if (!stack) {
			stack = document.createElement("div");
			stack.id = "app-alerts";
			stack.className = "app-alerts";
			stack.setAttribute("role", "status");
			document.body.appendChild(stack);
		}
		const alert = document.createElement("div");
		alert.className = `app-alert app-alert-${options.indicator || "blue"}`;
		alert.textContent = options.message || "";
		stack.appendChild(alert);
		setTimeout(() => alert.remove(), (seconds || 4) * 1000);
	}

	function call(opts) {
		if (typeof opts === "string") opts = { method: opts, args: arguments[1], callback: arguments[2] };
		const method = opts.method;
		const httpMethod = (opts.type || "POST").toUpperCase();
		const args = { ...(opts.args || {}) };
		if (opts.freeze) freeze(opts.freeze_message);

		let url = `/api/method/${method}`;
		const init = {
			method: httpMethod,
			credentials: "same-origin",
			headers: { Accept: "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token || "" },
		};
		if (httpMethod === "GET") {
			const query = new URLSearchParams();
			Object.entries(args).forEach(([key, value]) => {
				if (value === undefined || value === null) return;
				query.set(key, typeof value === "object" ? JSON.stringify(value) : value);
			});
			url += `?${query}`;
		} else {
			init.headers["Content-Type"] = "application/json";
			init.body = JSON.stringify(args);
		}

		const promise = fetch(url, init).then(async response => {
			let data = {};
			try { data = await response.json(); } catch (error) { data = {}; }
			if (!response.ok || data.exc_type) {
				const messages = serverMessages(data);
				if (response.status === 401 && !frappe.session.user_is_guest && !opts.silent_auth) {
					window.location.href = `/store-login?redirect-to=${encodeURIComponent(location.pathname + location.search)}`;
				}
				if (!opts.silent_errors) msgprint(messages.join("\n") || __("Something went wrong. Please try again."), __("Error"));
				const error = new Error(messages.join(" ") || response.statusText);
				error.response = data;
				error.status = response.status;
				opts.error?.(data, error);
				throw error;
			}
			opts.callback?.(data);
			return data;
		}, networkError => {
			msgprint(__("Could not reach the server. Check your connection and try again."), __("Offline"));
			opts.error?.({}, networkError);
			throw networkError;
		}).finally(() => {
			if (opts.freeze) unfreeze();
			opts.always?.();
		});
		// Callers using callbacks never await the promise; keep failures from surfacing as unhandled rejections.
		promise.catch(() => {});
		return promise;
	}

	function ready(fn) {
		if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
		else fn();
	}

	class FileUploader {
		constructor(options = {}) {
			const input = document.createElement("input");
			input.type = "file";
			const allowed = options.restrictions?.allowed_file_types;
			if (allowed?.length) input.accept = allowed.join(",");
			input.addEventListener("change", async () => {
				const file = input.files?.[0];
				if (!file) return;
				const body = new FormData();
				body.append("file", file, file.name);
				freeze(__("Uploading…"));
				try {
					const response = await fetch("/api/method/upload_file", {
						method: "POST", body, credentials: "same-origin",
						headers: { Accept: "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token || "" },
					});
					const data = await response.json();
					if (!response.ok || data.exc_type) throw new Error(serverMessages(data).join(" ") || __("Upload failed."));
					options.on_success?.(data.message);
				} catch (error) {
					msgprint(error.message, __("Upload failed"));
				} finally {
					unfreeze();
				}
			});
			input.click();
		}
	}

	const today = () => {
		const now = new Date();
		return new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
	};

	window.frappe = Object.assign(window.frappe || {}, {
		session: { user: boot.user || "Guest", user_fullname: boot.full_name || "", user_is_guest: !boot.user || boot.user === "Guest" },
		csrf_token: boot.csrf_token || "",
		boot: { lang: boot.lang || "en", sysdefaults: { company: boot.company || "" } },
		call, ready, msgprint, show_alert,
		ui: { FileUploader },
		utils: { escape_html: escapeHtml },
		datetime: {
			get_today: today,
			add_months: (value, months) => { const d = new Date(value); d.setMonth(d.getMonth() + Number(months || 0)); return d.toISOString().slice(0, 10); },
			add_days: (value, days) => { const d = new Date(value); d.setDate(d.getDate() + Number(days || 0)); return d.toISOString().slice(0, 10); },
		},
		defaults: { get_user_default: key => (key === "Company" ? boot.company || "" : "") },
		query_reports: {},
	});
})();
