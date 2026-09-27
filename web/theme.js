(() => {
 const root=document.documentElement,media=matchMedia('(prefers-color-scheme: dark)');
 let preference='system';try{const saved=localStorage.getItem('wb-theme');if(['light','dark','system'].includes(saved))preference=saved;}catch{}
 function apply(){root.dataset.theme=preference==='system'?(media.matches?'dark':'light'):preference;root.dataset.themePreference=preference;root.style.colorScheme=root.dataset.theme;window.dispatchEvent(new Event('wb-theme-change'));}
 window.WBTheme={get:()=>preference,set(value){if(!['light','dark','system'].includes(value))return;preference=value;try{localStorage.setItem('wb-theme',value);}catch{}apply();}};
 media.addEventListener('change',()=>{if(preference==='system')apply();});window.addEventListener('storage',e=>{if(e.key==='wb-theme'){preference=['light','dark','system'].includes(e.newValue)?e.newValue:'system';apply();}});apply();
})();
