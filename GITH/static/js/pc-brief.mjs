const form=document.getElementById('pc-brief');
if(form){
  const offer=form.elements.type_prestation,usage=form.elements.usage_pc,budget=form.elements.budget_materiel;
  const feedback=document.getElementById('pc-feedback');
  const values=()=>({type_prestation:offer.value,usage_pc:usage.value,budget_materiel:budget.value});
  const update=()=>{
    document.getElementById('pc-brief-price').textContent=`Accompagnement : ${offer.selectedOptions[0].dataset.price} € TTC`;
    document.getElementById('pc-brief-context').textContent=`${offer.value} · ${usage.value}`;
    feedback.textContent='Aucune commande ni aucun paiement sur cette page.';
  };
  form.addEventListener('change',update);update();
  form.addEventListener('submit',async event=>{
    event.preventDefault();const button=form.querySelector('[type=submit]');button.disabled=true;
    try{await window.ouvrirQuestionnaire('montage_pc',null,values());}
    catch{feedback.textContent='Le questionnaire ne se charge pas. Vous pouvez nous écrire à contact@numeryl.fr en reprenant ces repères.';}
    finally{button.disabled=false;}
  });
  document.getElementById('pc-save').addEventListener('click',()=>{
    const v=values();const text=`Numéryl - repères pour un conseil PC à distance\n\nAccompagnement : ${v.type_prestation}\nTarif de lancement : ${offer.selectedOptions[0].dataset.price} euros TTC\nUsage : ${v.usage_pc}\nBudget du matériel : ${v.budget_materiel}\n\nMatériel et licence Windows séparés. Aucun achat, commande ou envoi par ce fichier. Périmètre et disponibilité à confirmer lors du premier échange.\n`;
    const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='numeryl-mon-projet-pc.txt';a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);feedback.textContent='Fiche préparée sur votre appareil. Aucune donnée transmise.';
  });
}
