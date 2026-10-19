window.toggleMobileNav=()=>{const menu=document.getElementById('mobileNav');const open=menu.classList.toggle('actif');document.querySelector('.menu-toggle')?.setAttribute('aria-expanded',String(open));};
document.getElementById('year').textContent=String(new Date().getFullYear());
// Reuse the studio's existing menu and arrow glyphs on this information page.
const icons={menu:'<svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></svg>','arrow-right':'<svg viewBox="0 0 24 24"><path d="M4 12h16m-6-6 6 6-6 6"/></svg>',instagram:'<svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/></svg>'};
document.querySelectorAll('[data-icon]').forEach(el=>{el.innerHTML=icons[el.dataset.icon]||'';el.setAttribute('aria-hidden','true');});
