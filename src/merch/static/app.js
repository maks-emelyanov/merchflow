document.addEventListener("DOMContentLoaded", () => {
  const starter = document.getElementById("start-run");
  starter?.addEventListener("htmx:afterRequest", (event) => {
    if (event.detail.successful) window.setTimeout(() => window.location.reload(), 500);
  });

  const form = document.getElementById("approval-form");
  const setup = document.getElementById("connector-setup");
  if (setup) {
    const csrf = setup.dataset.csrf;
    document.getElementById("featured-variant")?.addEventListener("change", (event) => {
      const editor = document.getElementById("template-json");
      let template;
      try { template = JSON.parse(editor.value); }
      catch (_) { return window.alert("Fix the template JSON before choosing a featured photo."); }
      template.featured_variant_id = event.target.value ? Number(event.target.value) : null;
      editor.value = JSON.stringify(template, null, 2);
    });
    document.getElementById("save-template")?.addEventListener("click", async () => {
      let body;
      try { body = JSON.parse(document.getElementById("template-json").value); }
      catch (_) { return window.alert("Template JSON is invalid."); }
      const response = await fetch("/api/template", {method: "PUT", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: JSON.stringify(body)});
      if (!response.ok) return window.alert((await response.json()).detail || "Template save failed");
      window.location.reload();
    });
    document.getElementById("import-etsy")?.addEventListener("click", async () => {
      const file = document.getElementById("etsy-csv").files[0];
      if (!file) return window.alert("Choose an Etsy CSV first.");
      const data = new FormData(); data.append("file", file);
      const response = await fetch("/api/imports/etsy", {method: "POST", headers: {"X-CSRF-Token": csrf}, body: data});
      if (!response.ok) return window.alert((await response.json()).detail || "CSV import failed");
      window.alert(`Imported ${(await response.json()).imported_rows} rows.`);
    });
    document.getElementById("save-credential")?.addEventListener("click", async () => {
      const response = await fetch("/api/connectors/token", {method: "PUT", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: JSON.stringify({credential: document.getElementById("credential-name").value, value: document.getElementById("credential-value").value})});
      if (!response.ok) return window.alert((await response.json()).detail || "Credential save failed");
      window.alert("Encrypted credential stored.");
    });
  }
  const retryArtwork = document.getElementById("retry-artwork");
  retryArtwork?.addEventListener("click", async () => {
    let brief;
    try { brief = JSON.parse(document.getElementById("revised-brief-json").value); }
    catch (_) { return window.alert("Creative brief JSON is invalid."); }
    const response = await fetch(`/api/runs/${retryArtwork.dataset.runId}/retry-artwork`, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": retryArtwork.dataset.csrf},
      body: JSON.stringify(brief),
    });
    if (!response.ok) return window.alert((await response.json()).detail || "Artwork retry failed");
    window.location.reload();
  });
  document.querySelectorAll(".retry-channel").forEach((button) => {
    button.addEventListener("click", async () => {
      const response = await fetch(`/api/runs/${button.dataset.runId}/retry/${button.dataset.channel}`, {
        method: "POST", headers: {"X-CSRF-Token": button.dataset.csrf},
      });
      if (!response.ok) return window.alert((await response.json()).detail || "Verification retry failed");
      window.location.reload();
    });
  });
  const copyRefresh = document.getElementById("copy-refresh");
  if (copyRefresh) {
    const batchId = copyRefresh.dataset.batchId;
    const csrf = copyRefresh.dataset.csrf;
    const headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf};
    const handle = async (response) => {
      if (response.ok) return window.location.reload();
      let message = "Copy refresh request failed";
      try { message = (await response.json()).detail || message; } catch (_) {}
      window.alert(message);
    };
    document.getElementById("prepare-copy-refresh")?.addEventListener("click", async () => {
      const response = await fetch("/api/copy-refresh/prepare", {method: "POST", headers});
      await handle(response);
    });
    document.querySelectorAll(".copy-save").forEach((button) => button.addEventListener("click", async () => {
      const editor = button.closest(".copy-refresh-item");
      const response = await fetch(`/api/copy-refresh/${batchId}/items/${editor.dataset.itemId}`, {
        method: "PUT", headers,
        body: JSON.stringify({
          expected_version: Number(copyRefresh.dataset.version),
          title: editor.querySelector(".copy-title").value,
          long_description: editor.querySelector(".copy-description").value,
          tags: editor.querySelector(".copy-tags").value.split("\n").map((tag) => tag.trim()).filter(Boolean),
          alt_text: editor.querySelector(".copy-alt-text").value,
        }),
      });
      await handle(response);
    }));
    document.getElementById("approve-copy-refresh")?.addEventListener("click", async () => {
      if (!window.confirm("Apply every reviewed revision to the existing live listings?")) return;
      const response = await fetch(`/api/copy-refresh/${batchId}/approve`, {
        method: "POST", headers,
        body: JSON.stringify({expected_version: Number(copyRefresh.dataset.version), digest: copyRefresh.dataset.digest}),
      });
      await handle(response);
    });
    document.getElementById("retry-copy-refresh")?.addEventListener("click", async () => {
      const response = await fetch(`/api/copy-refresh/${batchId}/retry`, {method: "POST", headers});
      await handle(response);
    });
  }
  if (!form) return;
  const runId = form.dataset.runId;
  const version = Number(form.dataset.version);
  const csrf = form.dataset.csrf;

  async function checked(response, fallback) {
    if (response.ok) return true;
    let message = fallback;
    try { message = (await response.json()).detail || fallback; } catch (_) {}
    window.alert(message);
    return false;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const channels = [...document.querySelectorAll('input[name="channel"]:checked')].map(
      (element) => element.value,
    );
    if (!channels.length) return window.alert("Select at least one channel.");
    const response = await fetch(`/api/runs/${runId}/approve`, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
      body: JSON.stringify({
        channels,
        expected_version: version,
        ip_attested: document.getElementById("ip-attested")?.checked ?? false,
        confirmation: document.getElementById("confirmation").value,
      }),
    });
    if (await checked(response, "Approval failed")) window.location.reload();
  });

  document.querySelectorAll(".run-signal").forEach((button) => {
    button.addEventListener("click", async () => {
      const response = await fetch(`/api/runs/${runId}/${button.dataset.action}`, {
        method: "POST",
        headers: {"X-CSRF-Token": csrf},
      });
      if (await checked(response, "Action failed")) window.location.reload();
    });
  });

  document.getElementById("save-package")?.addEventListener("click", async () => {
    let body;
    try { body = JSON.parse(document.getElementById("package-json").value); }
    catch (_) { return window.alert("Package JSON is invalid."); }
    body.expected_version = version;
    const response = await fetch(`/api/runs/${runId}/package`, {
      method: "PUT",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
      body: JSON.stringify(body),
    });
    if (await checked(response, "Save failed")) window.location.reload();
  });
});
