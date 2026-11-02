(function () {
  const zh = (navigator.language || "").toLowerCase().startsWith("zh");
  document.documentElement.lang = zh ? "zh-CN" : "en";
  document.getElementById("token-label").textContent = zh ? "密码" : "Password";
  document.getElementById("submit").textContent = zh ? "登录" : "Sign in";
  if (new URLSearchParams(location.search).has("failed")) {
    const failed = document.getElementById("failed");
    failed.hidden = false;
    failed.textContent = zh ? "密码不对" : "That password is not right";
  }
})();
