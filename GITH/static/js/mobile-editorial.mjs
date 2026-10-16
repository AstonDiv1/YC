/* Progressive disclosure: original text nodes are moved, never shortened.
   Without JavaScript every original paragraph stays visible. */
const media = matchMedia('(max-width: 767px)');
const disclosures = [];
function fold(parent, nodes, label) {
  const content = nodes.filter(Boolean);
  if (!content.length) return;
  const details = document.createElement('details');
  details.className = 'mobile-detail';
  details.open = !media.matches;
  const summary = document.createElement('summary');
  summary.textContent = label;
  const body = document.createElement('div');
  body.className = 'mobile-detail-body';
  content[0].before(details);
  details.append(summary, body);
  content.forEach(node => body.append(node));
  disclosures.push(details);
}
document.querySelectorAll('.pillars .pillar, .process .step').forEach(card => {
  fold(card, [...card.querySelectorAll(':scope > p')], 'Voir le détail');
});
document.querySelectorAll('.services-grid .svc').forEach(card => {
  fold(card, [card.querySelector(':scope > ul')], 'Voir ce qui est inclus');
});
const story = document.querySelector('#histoire .histoire > div:first-child');
if (story) fold(story, [...story.querySelectorAll(':scope > p')].slice(1), 'Notre façon de travailler');
document.querySelectorAll('.portfolio-card').forEach(card => {
  const list = card.querySelector('ul');
  if (list) fold(list.parentElement, [list], 'Explorer les fonctionnalités');
});
document.querySelectorAll('.portfolio-depth article, .portfolio-method article').forEach(card => {
  fold(card, [...card.querySelectorAll(':scope > p')], 'Voir le détail');
});
const sync = () => disclosures.forEach(details => { details.open = !media.matches; });
media.addEventListener('change', sync);
document.documentElement.classList.add('editorial-enhanced');
