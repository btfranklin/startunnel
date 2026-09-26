const commandDialog = document.querySelector("#command-palette");
const commandOpeners = [...document.querySelectorAll("[data-command-open]")];
const commandQuery = document.querySelector("[data-command-query]");
const commandEmpty = document.querySelector("[data-command-empty]");
const commandClose = document.querySelector("[data-command-close]");
let commandReturnFocus = null;
let activeCommandIndex = 0;

const visibleCommandItems = () =>
  [...document.querySelectorAll("[data-command-item]")].filter((item) => !item.hidden);

const selectCommand = (index) => {
  const items = visibleCommandItems();
  if (!items.length) return;
  activeCommandIndex = (index + items.length) % items.length;
  items.forEach((item, itemIndex) => {
    if (itemIndex === activeCommandIndex) {
      item.setAttribute("aria-current", "true");
      item.scrollIntoView({ block: "nearest" });
    } else {
      item.removeAttribute("aria-current");
    }
  });
};

const filterCommands = () => {
  const query = commandQuery.value.trim().toLocaleLowerCase();
  const items = [...document.querySelectorAll("[data-command-item]")];
  items.forEach((item) => {
    item.hidden = query.length > 0 && !item.textContent.toLocaleLowerCase().includes(query);
  });
  document.querySelectorAll(".command-group").forEach((group) => {
    group.hidden = ![...group.querySelectorAll("[data-command-item]")].some((item) => !item.hidden);
  });
  commandEmpty.hidden = visibleCommandItems().length > 0;
  activeCommandIndex = 0;
  selectCommand(0);
};

const openCommands = (opener) => {
  if (!commandDialog || commandDialog.open) return;
  commandReturnFocus = opener || document.activeElement;
  commandDialog.showModal();
  commandQuery.value = "";
  filterCommands();
  commandQuery.focus();
};

const closeCommands = () => {
  if (commandDialog?.open) commandDialog.close();
};

commandOpeners.forEach((opener) => opener.addEventListener("click", () => openCommands(opener)));
commandClose?.addEventListener("click", closeCommands);
commandDialog?.addEventListener("click", (event) => {
  if (event.target === commandDialog) closeCommands();
});
commandDialog?.addEventListener("close", () => {
  if (commandReturnFocus instanceof HTMLElement) commandReturnFocus.focus();
});
commandQuery?.addEventListener("input", filterCommands);
commandQuery?.addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown") {
    event.preventDefault();
    selectCommand(activeCommandIndex + 1);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    selectCommand(activeCommandIndex - 1);
  } else if (event.key === "Enter") {
    const item = visibleCommandItems()[activeCommandIndex];
    if (item) {
      event.preventDefault();
      item.click();
    }
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && commandDialog?.open) {
    event.preventDefault();
    closeCommands();
  } else if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === "k") {
    event.preventDefault();
    commandDialog?.open ? closeCommands() : openCommands(null);
  }
});

const copyText = async (value) => {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const temporary = document.createElement("textarea");
  temporary.value = value;
  temporary.setAttribute("readonly", "");
  temporary.className = "visually-hidden";
  document.body.append(temporary);
  temporary.select();
  const copied = document.execCommand("copy");
  temporary.remove();
  if (!copied) throw new Error("The browser did not copy the value.");
};

document.querySelectorAll("[data-copy-target]").forEach((button) => {
  const status = document.createElement("span");
  status.className = "visually-hidden";
  status.setAttribute("aria-live", "polite");
  status.dataset.copyStatus = "";
  button.insertAdjacentElement("afterend", status);
  button.hidden = false;
  button.addEventListener("click", async () => {
    const target = document.getElementById(button.dataset.copyTarget);
    if (!target) return;
    try {
      await copyText(target.textContent.trim());
      button.dataset.state = "success";
      status.textContent = "Copied.";
      button.querySelector("[data-copy-error]")?.setAttribute("hidden", "");
      button.querySelector("[data-copy-default]")?.setAttribute("hidden", "");
      button.querySelector("[data-copy-success]")?.removeAttribute("hidden");
      window.setTimeout(() => {
        delete button.dataset.state;
        button.querySelector("[data-copy-default]")?.removeAttribute("hidden");
        button.querySelector("[data-copy-success]")?.setAttribute("hidden", "");
        status.textContent = "";
      }, 2500);
    } catch {
      button.dataset.state = "error";
      status.textContent = "Copy failed. Select and copy the value manually.";
      if (button.querySelector("[data-copy-error]")) {
        button.querySelector("[data-copy-default]")?.setAttribute("hidden", "");
        button.querySelector("[data-copy-success]")?.setAttribute("hidden", "");
        button.querySelector("[data-copy-error]")?.removeAttribute("hidden");
      }
    }
  });
});

document.querySelectorAll("[data-loading-form]").forEach((form) => {
  form.addEventListener("submit", () => {
    const button = form.querySelector("button[type='submit']");
    if (!button) return;
    button.setAttribute("aria-busy", "true");
    button.dataset.state = "loading";
    const label = button.dataset.loadingLabel;
    if (label) button.textContent = label;
  });
});

document.querySelectorAll("[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});

document.querySelector("[aria-invalid='true']")?.focus();

const stickyThreshold = document.querySelector("[data-sticky-threshold]");
const stickyAction = document.querySelector("[data-sticky-cta]");
if (stickyThreshold && stickyAction) {
  const observer = new IntersectionObserver(
    ([entry]) => {
      if (!entry.isIntersecting && entry.boundingClientRect.top < 0) stickyAction.removeAttribute("hidden");
      else stickyAction.setAttribute("hidden", "");
    },
    { threshold: 0 },
  );
  observer.observe(stickyThreshold);
}
