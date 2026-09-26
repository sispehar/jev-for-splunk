/* Setup page for jev_for_splunk: store the TypeSafe key in storage/passwords and run | jevtest. ES5 on purpose. */
require(["jquery", "splunkjs/mvc", "splunkjs/mvc/simplexml/ready!"], function ($, mvc) {
    var APP = "jev_for_splunk", REALM = "jev_for_splunk", USER = "typesafe_api_key";
    var service = mvc.createService({ owner: "nobody", app: APP });
    var ENTITY = REALM + ":" + USER + ":";

    function status(sel, text, kind) {
        $(sel).text(text).removeClass("ok warn fail").addClass(kind || "");
    }

    function refreshCurrent() {
        service.storagePasswords().fetch(function (err, coll) {
            if (err) {
                var code = err && err.status;
                $("#jev-key-current").text(code === 403 ? "cannot read (your role lacks list_storage_passwords)" : "unknown (" + (err.data && err.data.messages && err.data.messages[0] ? err.data.messages[0].text : code) + ")");
                return;
            }
            var item = coll.item(ENTITY);
            $("#jev-key-current").text(item ? "stored (realm " + REALM + ", user " + USER + ")" : "not stored yet");
            // a key stored some other way (REST, CLI, a restored backup) configures the app too;
            // otherwise Splunk keeps sending everyone who opens the app back to this page
            if (item) { markConfigured(); }
        });
    }

    function markConfigured() {
        service.post("apps/local/" + APP, { configured: true }, function () { /* best effort */ });
    }

    function errorText(err) {
        if (!err) { return "unknown error"; }
        if (err.data && err.data.messages && err.data.messages.length) { return err.data.messages[0].text; }
        return "HTTP " + err.status;
    }

    $("#jev-save").on("click", function () {
        var key = ($("#jev-key").val() || "").trim();
        if (!key) { status("#jev-key-status", "paste a key first", "warn"); return; }
        status("#jev-key-status", "saving…", "");
        service.storagePasswords().fetch(function (err, coll) {
            if (err) { status("#jev-key-status", "cannot access the credential store: " + errorText(err), "fail"); return; }
            var create = function () {
                coll.create({ name: USER, realm: REALM, password: key }, function (err2) {
                    if (err2) { status("#jev-key-status", "save failed: " + errorText(err2), "fail"); return; }
                    $("#jev-key").val("");
                    status("#jev-key-status", "saved", "ok");
                    markConfigured();
                    refreshCurrent();
                });
            };
            var existing = coll.item(ENTITY);
            if (existing) { existing.remove(function () { create(); }); } else { create(); }
        });
    });

    $("#jev-test").on("click", function () {
        status("#jev-test-status", "running | jevtest live=true …", "");
        $("#jev-test-table tbody").empty();
        service.oneshotSearch("| jevtest live=true", { output_mode: "json_rows", count: 0 }, function (err, results) {
            if (err) { status("#jev-test-status", "search failed: " + errorText(err), "fail"); return; }
            var fields = results.fields || [], rows = results.rows || [];
            var ci = fields.indexOf("check"), si = fields.indexOf("status"), di = fields.indexOf("detail");
            var worst = "ok";
            $.each(rows, function (_, row) {
                var st = row[si] || "";
                if (st === "fail") { worst = "fail"; } else if (st === "warn" && worst !== "fail") { worst = "warn"; }
                $("#jev-test-table tbody").append(
                    $("<tr/>").append($("<td/>").text(row[ci])).append($("<td/>").addClass(st).text(st)).append($("<td/>").text(row[di]))
                );
            });
            status("#jev-test-status", rows.length + " checks, worst status: " + worst, worst);
        });
    });

    // Settings: read the effective jev.conf values; write only the ones changed, so an app upgrade's new
    // defaults are not hidden behind copies in local/jev.conf.
    var VALID = {
        model: /^[A-Za-z0-9._-]+$/,
        maxevents: /^\d+$/,
        ttl_days: /^\d+$/,
        proxy_url: /^(https?:\/\/\S+)?$/
    };

    function settingInputs() {
        return $("#jev-settings-save").closest(".jev-setup").find("input[data-stanza]");
    }

    function loadSettings() {
        var stanzas = {};
        settingInputs().each(function () { stanzas[$(this).data("stanza")] = true; });
        $.each(stanzas, function (stanza) {
            service.get("configs/conf-jev/" + stanza, { output_mode: "json" }, function (err, response) {
                if (err) { status("#jev-settings-status", "cannot read jev.conf: " + errorText(err), "warn"); return; }
                var entry = response && response.data && response.data.entry && response.data.entry[0];
                var content = (entry && entry.content) || {};
                settingInputs().filter("[data-stanza='" + stanza + "']").each(function () {
                    var value = content[$(this).data("key")];
                    value = (value === undefined || value === null) ? "" : String(value);
                    $(this).val(value).data("loaded", value);
                });
            });
        });
    }

    $("#jev-settings-save").on("click", function () {
        var changes = {}, count = 0, bad = [];
        settingInputs().each(function () {
            var key = $(this).data("key"), stanza = $(this).data("stanza"), value = ($(this).val() || "").trim();
            if (value === $(this).data("loaded")) { return; }
            if (VALID[key] && !VALID[key].test(value)) { bad.push(key); return; }
            changes[stanza] = changes[stanza] || {};
            changes[stanza][key] = value;
            count += 1;
        });
        if (bad.length) { status("#jev-settings-status", "check " + bad.join(", "), "warn"); return; }
        if (!count) { status("#jev-settings-status", "nothing changed", ""); return; }
        status("#jev-settings-status", "saving…", "");
        var stanzas = Object.keys(changes), pending = stanzas.length, failure = null;
        $.each(stanzas, function (_, stanza) {
            service.post("configs/conf-jev/" + stanza, changes[stanza], function (err) {
                if (err) {
                    failure = failure || errorText(err);
                } else {
                    $.each(changes[stanza], function (key, value) {
                        settingInputs().filter("[data-key='" + key + "']").data("loaded", value);
                    });
                }
                pending -= 1;
                if (pending === 0) {
                    status("#jev-settings-status", failure ? "save failed: " + failure : "saved; the next search uses it",
                           failure ? "fail" : "ok");
                }
            });
        });
    });

    refreshCurrent();
    loadSettings();
});
