const header = document.querySelector(".site-nav");
const slot = document.querySelector("[data-header-prompt]");
const button = document.querySelector(".signal-hero [data-copy-target]");

if (header && slot && button) {
  let observer;
  const observe = () => {
    observer?.disconnect();
    const headerHeight = header.getBoundingClientRect().height;
    observer = new IntersectionObserver(([entry]) => {
      const visible = !entry.isIntersecting && entry.boundingClientRect.bottom <= headerHeight;
      slot.classList.toggle("site-nav__prompt--visible", visible);
      slot.inert = !visible;
      slot.setAttribute("aria-hidden", String(!visible));
    }, { rootMargin: `-${headerHeight}px 0px 0px 0px` });
    observer.observe(button);
  };
  new ResizeObserver(observe).observe(header);
  observe();
}
