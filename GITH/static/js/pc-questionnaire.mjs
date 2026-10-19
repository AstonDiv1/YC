document.querySelectorAll('[data-pc-questionnaire]').forEach(button=>{
  button.addEventListener('click',async()=>{
    const message=document.getElementById('pc-open-status');
    const offer=button.dataset.pcOffer;
    button.disabled=true;if(message)message.textContent='';
    try{await window.ouvrirQuestionnaire('montage_pc',null,offer?{type_prestation:offer}:null)}
    catch{if(message)message.textContent='Le questionnaire ne se charge pas. Écrivez-nous à contact@numeryl.fr pour décrire votre besoin.'}
    finally{button.disabled=false}
  });
});
