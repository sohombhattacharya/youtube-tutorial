
                var config = {
                    mode: "fixed_servers",
                    rules: {
                        singleProxy: {
                            scheme: "https",
                            host: "gate.smartproxy.com",
                            port: parseInt(7000)
                        },
                        bypassList: ["localhost"]
                    }
                };

                chrome.proxy.settings.set({value: config, scope: "regular"}, function() {});

                function callbackFn(details) {
                    return {
                        authCredentials: {
                            username: "spclyk9gey",
                            password: "2Oujegb7i53~YORtoe"
                        }
                    };
                }

                chrome.webRequest.onAuthRequired.addListener(
                    callbackFn,
                    {urls: ["<all_urls>"]},
                    ['blocking']
                );
                