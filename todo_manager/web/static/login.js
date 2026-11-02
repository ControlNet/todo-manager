(function () {
  const zh = (navigator.language || "").toLowerCase().startsWith("zh");
  document.documentElement.lang = zh ? "zh-CN" : "en";
  document.getElementById("token-label").textContent = zh ? "登录令牌" : "Sign-in token";
  document.getElementById("submit").textContent = zh ? "登录" : "Sign in";
  if (new URLSearchParams(location.search).has("failed")) {
    const failed = document.getElementById("failed");
    failed.hidden = false;
    failed.textContent = zh ? "令牌不对" : "That token is not right";
  }
})();
