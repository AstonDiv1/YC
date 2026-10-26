const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const active = new Set();
const byElement = new WeakMap();
const seen = new WeakSet();

function enter(element, delay = 0) {
  if (reduced.matches || !element.animate || element.hidden) return;
  byElement.get(element)?.cancel();
  const animation = element.animate([
    {opacity: .75, transform: 'translateY(10px)'},
    {opacity: 1, transform: 'none'},
  ], {duration: 440, delay, easing: 'cubic-bezier(.22,1,.36,1)'});
  active.add(animation); byElement.set(element, animation);
  const clean = () => { active.delete(animation); if (byElement.get(element) === animation) byElement.delete(element); };
  animation.onfinish = clean; animation.oncancel = clean;
}

const selector = '.hero .eyebrow,.hero h1,.hero .lead,.hero-actions,.hero-meta,.proposal-visual,.section-head,.pillar,.svc,.workflow-card,.founders,.contact-info,.contact-form,.portfolio-intro,.portfolio-toolbar,.portfolio-card,.depth-heading,.depth-grid article,.method-grid article,.portfolio-end';
let observer;
if ('IntersectionObserver' in window && !reduced.matches) {
  observer = new IntersectionObserver(entries => {
    let order = 0;
    for (const entry of entries) {
      if (!entry.isIntersecting || seen.has(entry.target)) continue;
      seen.add(entry.target); observer.unobserve(entry.target);
      enter(entry.target, Math.min(order++, 3) * 35);
    }
  }, {threshold: .05, rootMargin: '0px 0px 35px 0px'});
  document.querySelectorAll(selector).forEach(element => observer.observe(element));
}

document.querySelectorAll('[data-portfolio-filter]').forEach(button => {
  button.addEventListener('click', () => {
    if (reduced.matches) return;
    queueMicrotask(() => {
      if (reduced.matches) return;
      active.forEach(animation => animation.cancel());
      document.querySelectorAll('.portfolio-card:not([hidden])').forEach((card, index) => enter(card, Math.min(index, 3) * 35));
    });
  });
});
reduced.addEventListener('change', () => {
  if (reduced.matches) { active.forEach(animation => animation.cancel()); observer?.disconnect(); }
});
document.addEventListener('focusin', event => {
  const parent = event.target.closest(selector);
  if (parent) byElement.get(parent)?.cancel();
});
