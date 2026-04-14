(function () {
  function escapeHtml(value) {
    const element = document.createElement("div");
    element.textContent = String(value || "");
    return element.innerHTML;
  }

  function ensureTopNotice(message, level) {
    const subnav = document.querySelector(".subnav");
    if (!subnav) return null;

    let notice = document.querySelector(".subnav + .notice[data-generated-notice='true']");
    if (!notice) {
      notice = document.createElement("div");
      notice.setAttribute("data-generated-notice", "true");
      subnav.insertAdjacentElement("afterend", notice);
    }

    notice.className = "notice notice-" + (level || "success");
    notice.innerHTML =
      "<strong>" +
      ((level || "success") === "danger"
        ? "Error:"
        : (level || "success") === "warning"
        ? "Warning:"
        : "Success:") +
      "</strong> " +
      escapeHtml(message || "");
    return notice;
  }

  function renderQueueTable(container, rows) {
    if (!container) return;
    if (!rows || !rows.length) {
      container.innerHTML = '<tr><td colspan="12"><div class="empty-state"><h3>No outbound queue items yet</h3><p>Voice delivery jobs will appear here when SMS-triggered calls are queued or retried.</p></div></td></tr>';
      return;
    }

    container.innerHTML = rows
      .map(function (item) {
        const audioCell = item.audio_path
          ? '<button type="button" class="button button-secondary button-small" title="Play generated audio" aria-label="Play generated audio for queued message" onclick="window.__adminPlayQueueAudio(\'/admin/queue/audio/' + escapeHtml(item.id) + '\', this)">▶</button>'
          : '<span class="muted">—</span>';
        return (
          "<tr>" +
          '<td><input type="checkbox" name="item_ids" value="' + escapeHtml(item.id) + '" /></td>' +
          '<td class="mono">' + escapeHtml(item.created_at) + "</td>" +
          '<td class="mono">' + escapeHtml(item.phone_number || "—") + "</td>" +
          "<td>" + escapeHtml(item.provider || "—") + "</td>" +
          '<td><span class="status-badge status-' + escapeHtml(item.status_class) + '">' + escapeHtml(item.status) + "</span></td>" +
          '<td class="mono">' + escapeHtml(item.attempts + "/" + item.max_attempts) + "</td>" +
          '<td class="mono">' + escapeHtml(item.retry_interval_seconds + "s") + "</td>" +
          '<td class="mono">' + escapeHtml(item.next_attempt_at || "—") + "</td>" +
          '<td class="mono">' + escapeHtml(item.sip_call_id || item.sip_account_id || item.ami_action_id || "—") + "</td>" +
          "<td>" + audioCell + "</td>" +
          '<td class="message-preview">' + escapeHtml(item.body_preview || item.body || "—") + "</td>" +
          "<td>" + escapeHtml(item.last_error || "—") + "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  async function refreshQueue() {
    const tableBody = document.getElementById("live-queue-body");
    const queueCount = document.querySelector(".queue-summary-count");
    const queueActive = document.querySelector(".queue-summary-active");
    try {
      const response = await fetch("/admin/reports/live", {
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (!response.ok) return;
      const payload = await response.json();
      renderQueueTable(tableBody, payload.recent_queue_items || []);
      if (queueCount) queueCount.textContent = String((payload.queue_summary && payload.queue_summary.total) || 0);
      if (queueActive) queueActive.textContent = (payload.queue_summary && payload.queue_summary.active_label) || "Monitoring";
    } catch (error) {
      console.debug("Queue refresh failed", error);
    }
  }

  function initAutoRefresh() {
    const intervalMs = 3000;
    refreshQueue();
    window.setInterval(refreshQueue, intervalMs);
  }

  document.addEventListener("DOMContentLoaded", function () {
    initAutoRefresh();
  });

  window.__adminEnsureTopNotice = ensureTopNotice;
})();
