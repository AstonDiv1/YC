const mobile=matchMedia('(max-width: 767px)');
const reduced=matchMedia('(prefers-reduced-motion: reduce)');

// Reveal just before a section enters the screen, rather than leaving a blank area.
let observer;
function revealMobile(){
  observer?.disconnect();
  if(!mobile.matches)return;
  const elements=document.querySelectorAll('.reveal');
  if(reduced.matches||!('IntersectionObserver' in window)){
    elements.forEach(element=>element.classList.add('visible'));return;
  }
  observer=new IntersectionObserver(entries=>entries.forEach(entry=>{
    if(entry.isIntersecting){entry.target.classList.add('visible');observer.unobserve(entry.target)}
  }),{threshold:.01,rootMargin:'0px 0px 80px 0px'});
  elements.forEach(element=>{
    if(element.getBoundingClientRect().top<innerHeight)element.classList.add('visible');
    else if(!element.classList.contains('visible'))observer.observe(element);
  });
}
mobile.addEventListener('change',revealMobile);
reduced.addEventListener('change',revealMobile);
revealMobile();

// Native details remains the fallback. Rapid reversals start at the current height.
document.querySelectorAll('details.mobile-detail,#conseil-pc details').forEach(details=>{
  const summary=details.querySelector(':scope > summary');
  if(!summary)return;
  let animation=null,target=details.open;
  const settle=()=>{
    animation?.cancel();animation=null;details.style.overflow='';
    if(!mobile.matches&&details.classList.contains('mobile-detail'))target=true;
    details.open=target;summary.setAttribute('aria-expanded',String(target));
  };
  summary.setAttribute('aria-expanded',String(details.open));
  summary.addEventListener('click',event=>{
    if(!mobile.matches||reduced.matches||!details.animate)return;
    event.preventDefault();
    const start=details.getBoundingClientRect().height;
    target=animation?!target:!details.open;
    animation?.cancel();details.open=target;
    const end=details.getBoundingClientRect().height;
    details.open=true;
    details.style.overflow='hidden';summary.setAttribute('aria-expanded',String(target));
    const running=details.animate([{height:start+'px'},{height:end+'px'}],{duration:240,easing:'cubic-bezier(.22,1,.36,1)',fill:'both'});
    animation=running;
    running.onfinish=()=>{if(animation===running)settle()};
  });
  details.addEventListener('toggle',()=>{if(!animation){target=details.open;summary.setAttribute('aria-expanded',String(target))}});
  mobile.addEventListener('change',()=>{if(details.classList.contains('mobile-detail'))target=!mobile.matches;settle()});
  reduced.addEventListener('change',()=>{if(reduced.matches)settle()});
});

const menu=document.getElementById('mobileNav'),menuButton=document.querySelector('.menu-toggle');
if(menu&&menuButton){
  const sync=()=>{menu.inert=!menu.classList.contains('actif')};
  const close=()=>{menu.classList.remove('actif');menuButton.setAttribute('aria-expanded','false');sync()};
  sync();menuButton.addEventListener('click',sync);
  menu.querySelectorAll('a').forEach(link=>link.addEventListener('click',sync));
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&menu.classList.contains('actif')){close();menuButton.focus()}});
  document.addEventListener('pointerdown',event=>{if(mobile.matches&&menu.classList.contains('actif')&&!menu.contains(event.target)&&!menuButton.contains(event.target))close()});
}
