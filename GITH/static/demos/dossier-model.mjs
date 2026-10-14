export const MATERIALS = {oak:'Chêne naturel', light:'Bois clair', painted:'Laque ivoire', undecided:'À définir ensemble'};
export const TYPES = {library:'Bibliothèque', wardrobe:'Rangement d’entrée', desk:'Bureau sur mesure'};
export const CASES = [
  {id:'01', title:'La bibliothèque du salon', type:'library', room:'Salon', need:'Ranger les livres et garder une place pour quelques objets, avec des portes en partie basse.',width:'280',height:'240',depth:'35',material:'oak',timing:'À discuter',photos:[],confirmed:false},
  {id:'02', title:'Une entrée mieux organisée', type:'wardrobe', room:'Entrée', need:'Réunir manteaux, chaussures et sacs dans un meuble adapté à une petite entrée.',width:'180',height:'',depth:'55',material:'undecided',timing:'Dans les prochains mois',photos:[],confirmed:false},
  {id:'03', title:'Le bureau sous la fenêtre', type:'desk', room:'Bureau',need:'Un plan de travail pour deux écrans, avec des rangements et un passage pour les câbles.',width:'',height:'',depth:'',material:'light',timing:'À discuter',photos:[],confirmed:false}
];
export const clone = value => JSON.parse(JSON.stringify(value));
export function validate(draft) {
  const errors = {};
  for (const field of ['width','height','depth']) {
    const raw = String(draft[field] ?? '').trim();
    if(raw && (!/^\d+(?:[.,]\d+)?$/.test(raw) || Number(raw.replace(',','.')) <= 0 || Number(raw.replace(',','.')) > 1000)) errors[field] = 'Saisissez une dimension entre 0 et 1 000 cm, ou laissez vide.';
  }
  if(String(draft.need ?? '').length > 2500) errors.need = 'Pour cet essai, gardez un besoin de moins de 2 500 caractères.';
  if(!Object.hasOwn(TYPES,draft.type)) errors.type = 'Choisissez un type de projet proposé.';
  if(!Object.hasOwn(MATERIALS,draft.material)) errors.material = 'Choisissez une finition proposée.';
  return errors;
}
export function checklist(project) {
  const errors = validate(project);
  return [
    {id:'need',label:'Un besoin expliqué',ok:String(project.need ?? '').trim().length >= 20&&!errors.need},
    {id:'dimensions',label:'Des dimensions indicatives',ok:['width','height','depth'].every(k=>String(project[k]??'').trim()&&!errors[k])},
    {id:'photos',label:'Une photo de l’espace',ok:project.photos.length>0},
    {id:'material',label:'Une préférence de finition',ok:project.material!=='undecided'},
    {id:'confirmed',label:'Les préférences confirmées',ok:Boolean(project.confirmed)}
  ];
}
export function createProject(index=0) {
  const draft = clone(CASES[index]);
  return {draft,versions:[{number:1,at:null,data:clone(draft),changes:['Exemple de départ']}]};
}
export function changedFields(previous,next) {
  const labels={need:'Besoin',width:'Largeur',height:'Hauteur',depth:'Profondeur',material:'Finition',timing:'Période',photos:'Photos',confirmed:'Confirmation des préférences'};
  return Object.entries(labels).filter(([key])=>JSON.stringify(previous[key])!==JSON.stringify(next[key])).map(([,label])=>label);
}
export function saveVersion(state,at=new Date().toISOString()) {
  const errors=validate(state.draft);
  if(Object.keys(errors).length) return {ok:false,errors};
  const changes=changedFields(state.versions.at(-1).data,state.draft);
  if(!changes.length) return {ok:false,unchanged:true};
  const version={number:state.versions.at(-1).number+1,at,data:clone(state.draft),changes};
  state.versions.push(version);
  return {ok:true,version};
}
export function updateField(state,key,value) {
  if(!['need','width','height','depth','material','timing','photos','confirmed'].includes(key)) return;
  if(key!=='confirmed' && JSON.stringify(state.draft[key])!==JSON.stringify(value)) state.draft.confirmed=false;
  state.draft[key]=clone(value);
}
export function requestText(project) {
  const missing=checklist(project).filter(c=>!c.ok);
  const questions={need:'Pouvez-vous préciser ce que vous souhaitez ranger ou utiliser ?',dimensions:'Pouvez-vous ajouter des dimensions approximatives, si vous les connaissez ?',photos:'Pouvez-vous ajouter une photo de l’espace et de ses contraintes ?',material:'Avez-vous une préférence de finition ou souhaitez-vous en discuter ?',confirmed:'Confirmez-vous que les préférences du dossier correspondent à votre idée actuelle ?'};
  return 'Bonjour,\n\nPour préparer notre échange sur votre projet :\n'+(missing.length?missing.map(c=>'• '+questions[c.id]).join('\n'):'Les premières informations sont renseignées. Nous pouvons préciser ensemble les contraintes et les prochaines étapes.')+'\n\nLes dimensions resteront à relever et à valider par l’atelier.\n\nMerci !';
}
export function exportText(version) {
  const p=version.data;
  return ['NUMÉRYL — DOSSIER ATELIER / DÉMONSTRATION','Données fictives ou saisies dans cet onglet. Aucun envoi à un atelier.',`Dossier ${p.id} · ${p.title} · version ${version.number}`,'',`Projet : ${TYPES[p.type]}`,`Pièce : ${p.room}`,`Besoin : ${p.need||'À préciser'}`,`Dimensions indicatives (cm) : L ${p.width||'?'} × H ${p.height||'?'} × P ${p.depth||'?'}`,`Finition : ${MATERIALS[p.material]}`,`Période : ${p.timing}`,`Photos : ${p.photos.length?p.photos.map(f=>f.name).join(', '):'À ajouter'}`,'','PRÉPARATION DU PREMIER ÉCHANGE',...checklist(p).map(c=>`${c.ok?'Renseigné':'À compléter'} : ${c.label}`),'','Relevé et validation technique par le professionnel : à réaliser.','Ce récapitulatif ne constitue ni un devis, ni un plan de fabrication, ni une commande acceptée.'].join('\n');
}
