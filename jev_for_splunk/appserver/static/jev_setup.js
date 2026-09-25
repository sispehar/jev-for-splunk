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

    refreshCurrent();
});
