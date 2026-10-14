import test from 'node:test';
import assert from 'node:assert/strict';
import {createProject,updateField,saveVersion,validate,checklist,requestText,exportText} from './static/demos/dossier-model.mjs';

test('recording a version preserves the previous snapshot and exports the saved choice',()=>{
  const s=createProject();updateField(s,'material','painted');
  assert.equal(saveVersion(s,'2026-10-14T08:00:00Z').ok,true);
  updateField(s,'material','light');
  assert.equal(s.versions[0].data.material,'oak');
  assert.equal(s.versions[1].data.material,'painted');
  assert.match(exportText(s.versions[1]),/Laque ivoire/);
  assert.doesNotMatch(exportText(s.versions[1]),/Finition : Bois clair/);
});
test('invalid dimensions cannot create a new version',()=>{
  for(const value of ['-1','0','1001','12 cm','NaN','Infinity','1e3']){
    const s=createProject();updateField(s,'width',value);
    assert.ok(validate(s.draft).width,value);assert.equal(saveVersion(s).ok,false);assert.equal(s.versions.length,1);
  }
});
test('missing dimensions remain allowed but explicitly incomplete',()=>{
  const s=createProject(2);assert.deepEqual(validate(s.draft),{});
  assert.equal(checklist(s.draft).find(c=>c.id==='dimensions').ok,false);
  assert.match(exportText(s.versions[0]),/L \? × H \? × P \?/);
});
test('decimal commas and upper boundary are accepted',()=>{
  const s=createProject();updateField(s,'width','120,5');updateField(s,'depth','1000');
  assert.deepEqual(validate(s.draft),{});assert.equal(saveVersion(s).ok,true);
});
test('changing a preference invalidates confirmation but repeating a value does not',()=>{
  const s=createProject();updateField(s,'confirmed',true);updateField(s,'material','oak');assert.equal(s.draft.confirmed,true);
  updateField(s,'material','light');assert.equal(s.draft.confirmed,false);
  updateField(s,'confirmed',true);updateField(s,'photos',[{name:'exemple.png'}]);assert.equal(s.draft.confirmed,false);
});
test('saving without changes never duplicates a version',()=>{
  const s=createProject();assert.equal(saveVersion(s).unchanged,true);assert.equal(s.versions.length,1);
});
test('project edits and photos are isolated between cases and historical versions',()=>{
  const first=createProject(),second=createProject();updateField(first,'photos',[{name:'local.png'}]);saveVersion(first);
  first.draft.photos[0].name='changed.png';
  assert.equal(first.versions.at(-1).data.photos[0].name,'local.png');assert.equal(second.draft.photos.length,0);
});
test('request drafts only ask for missing information and retain professional measurement',()=>{
  const s=createProject();updateField(s,'photos',[{name:'local.png'}]);
  assert.doesNotMatch(requestText(s.draft),/ajouter une photo/);
  assert.match(requestText(s.draft),/Confirmez-vous/);
  assert.match(requestText(s.draft),/valider par l’atelier/);
  updateField(s,'width','');assert.match(requestText(s.draft),/dimensions approximatives/);
});
test('full preliminary information does not remove the professional verification warning',()=>{
  const s=createProject();updateField(s,'photos',[{name:'local.png'}]);updateField(s,'confirmed',true);saveVersion(s);
  assert.equal(checklist(s.draft).filter(c=>c.ok).length,5);
  const text=exportText(s.versions.at(-1));assert.match(text,/validation technique.*à réaliser/);assert.match(text,/ni une commande acceptée/);
});
test('unexpected enum values fail validation and unsupported fields cannot be changed',()=>{
  const s=createProject();updateField(s,'id','other');assert.equal(s.draft.id,'01');
  updateField(s,'material','unexpected');assert.ok(validate(s.draft).material);
});
