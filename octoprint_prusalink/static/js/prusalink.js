$(function () {
    function PrusaLinkViewModel(parameters) {
        var self = this;
        self.settingsViewModel = parameters[0];
        self.settings = undefined;

        self.testing = ko.observable(false);
        self.testOk = ko.observable(false);
        self.testResult = ko.observable("");

        self.onBeforeBinding = function () {
            self.settings = self.settingsViewModel.settings;
        };

        self.testConnection = function () {
            var s = self.settings.plugins.prusalink;
            self.testing(true);
            self.testResult("");
            OctoPrint.simpleApiCommand("prusalink", "test", {
                host: s.host(),
                username: s.username(),
                password: s.password(),
                api_key: s.api_key(),
                use_https: s.use_https(),
                verify_tls: s.verify_tls(),
                request_timeout: s.request_timeout()
            })
                .done(function (r) {
                    self.testOk(!!r.ok);
                    if (r.ok) {
                        self.testResult(
                            "OK: " + (r.printer || "printer") + " fw " + (r.firmware || "?") +
                            ", state " + (r.state || "?") +
                            ", storage " + ((r.storage || []).join(", ") || "none")
                        );
                    } else {
                        self.testResult("Failed: " + r.error);
                    }
                })
                .fail(function () {
                    self.testOk(false);
                    self.testResult("Failed: request error");
                })
                .always(function () {
                    self.testing(false);
                });
        };

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "prusalink" || !data || !data.text) return;
            new PNotify({
                title: "PrusaLink",
                text: data.text,
                type: data.type === "error" ? "error" : "info",
                hide: data.type !== "error"
            });
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: PrusaLinkViewModel,
        dependencies: ["settingsViewModel"],
        elements: ["#settings_plugin_prusalink"]
    });
});
