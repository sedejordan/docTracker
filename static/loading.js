// Fixes the frozen loading screen
window.addEventListener("pageshow", function (e) {
  if (e.persisted) {
    const overlay = document.getElementById("loading-overlay");
    const ring = document.getElementById("loading-ring");
    overlay.classList.add("hidden");
    overlay.classList.remove("flex");
    ring.style.strokeDashoffset = "81.68";
  }
});

// Shows the branded loading overlay for anything that leaves the current
// page: a form submit, or clicking a link to another page on this site.
// Doesn't (and can't) cover the very first request when the page hasn't
// loaded yet - that part is the browser waiting for a response, before
// any of this JS has had a chance to run.
document.addEventListener("DOMContentLoaded", function () {
  const overlay = document.getElementById("loading-overlay");
  const ring = document.getElementById("loading-ring");

  function showLoading() {
    overlay.classList.remove("hidden");
    overlay.classList.add("flex");
    // Runs on the next frame so the browser registers the starting
    // dashoffset (81.68, i.e. empty) before animating to 0 (full) -
    // without this the ring would just snap to full instantly instead
    // of sweeping around.
    requestAnimationFrame(function () {
      ring.style.strokeDashoffset = "0";
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
    // Skip links that have a download attribute (file downloads)
    if (link.hasAttribute('download')) {
      return;
    }

    link.addEventListener("click", function (e) {
      // Skip if the user is opening it in a new tab/window, or if the
      // link's own handler already cancelled the navigation (e.g. the
      // delete confirm() dialog was dismissed with Cancel).
      if (e.defaultPrevented) {
        return;
      }
      if (!e.metaKey && !e.ctrlKey && !e.shiftKey) {
        showLoading();
      }
    });
  });
});
