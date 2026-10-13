// Shared, interruptible disclosure animation. Native <details> works without JS.
export function enhanceDisclosures(root = document) {
  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  root.querySelectorAll('details').forEach(details => {
    const summary = details.querySelector('summary');
    if (!summary || summary.dataset.enhanced) return;
    summary.dataset.enhanced = 'true';
    const icon = document.createElement('span');
    icon.className = 'disclosure-icon'; icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>';
    summary.append(icon);
    const content = document.createElement('div');
    content.className = 'accordion-content';
    [...details.childNodes].filter(node => node !== summary).forEach(node => content.append(node));
    details.append(content);
    let animation, target = details.open;
    summary.setAttribute('aria-expanded', String(target));
    summary.addEventListener('click', event => {
      event.preventDefault();
      const start = details.getBoundingClientRect().height;
      target = !target;
      animation?.cancel();
      summary.setAttribute('aria-expanded', String(target));
      if (reduced() || !details.animate) { details.open = target; return; }
      details.open = true;
      const end = target ? summary.offsetHeight + content.offsetHeight : summary.offsetHeight;
      details.style.overflow = 'hidden';
      animation = details.animate([{height: start + 'px'}, {height: end + 'px'}], {
        duration: 320, easing: 'cubic-bezier(.22,.68,.15,1)', fill: 'both'
      });
      animation.onfinish = () => {
        details.open = target;
        details.style.overflow = '';
        animation.cancel(); animation = null;
      };
    });
  });
}

export function revealElements(selector, root = document) {
  if (!('IntersectionObserver' in window) || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const observer = new IntersectionObserver(entries => entries.forEach(entry => {
    if (entry.isIntersecting) { entry.target.classList.remove('pending'); observer.unobserve(entry.target); }
  }), {threshold: .08});
  root.querySelectorAll(selector).forEach(element => {
    element.classList.add('reveal', 'pending'); observer.observe(element);
  });
}
