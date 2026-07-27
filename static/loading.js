// Shows the branded loading overlay for anything that leaves the current
// page: a form submit, or clicking a link to another page on this site.
// Doesn't (and can't) cover the very first request when the page hasn't
// loaded yet - that part is the browser waiting for a response, before
// any of this JS has had a chance to run.
document.addEventListener("DOMContentLoaded", function () {
  const overlay = document.getElementById("loading-overlay");
  const fill = document.getElementById("loading-fill");

  function showLoading() {
    overlay.classList.remove("hidden");
    overlay.classList.add("flex");
    // Runs on the next frame so the browser registers the starting
    // width (0) before animating to 100 - without this the fill would
    // just snap to full instantly instead of animating.
    requestAnimationFrame(function () {
      fill.style.width = "100%";
    });
  }

  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function () {
      if (form.checkValidity()) {
        showLoading();
      }
    });
  });

  document.querySelectorAll('a[href^="/"]').forEach(function (link) {
    link.addEventListener("click", function (e) {
      // Skip if the user is opening it in a new tab/window.
      if (!e.metaKey && !e.ctrlKey && !e.shiftKey) {
        showLoading();
      }
    });
  });
});
