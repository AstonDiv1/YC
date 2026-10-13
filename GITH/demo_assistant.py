"""Bounded conversational demo: intents, follow-up state, local page context.

No model/API/tool execution, external retrieval or server-side chat storage.
Knowledge comes only from the fictional restaurant's authored catalogue.
"""
from __future__ import annotations
import difflib
import json
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path

CATALOGUE = json.loads((Path(__file__).parent / 'static/demos/assets/restaurant-menu.json').read_text(encoding='utf-8'))
INTENTS = {'hours', 'booking', 'cancel', 'menu', 'allergens', 'selection', 'address', 'demo', 'identity'}
PAGES = {'home': 'l’accueil', 'menu': 'la carte', 'reservation': 'la réservation'}
MODE = 'Assistant local à intentions et contexte, sans modèle génératif'

def normalize(value):
    text = unicodedata.normalize('NFKD', str(value).lower())
    return re.sub(r'[^a-z0-9\s]', ' ', ''.join(c for c in text if not unicodedata.combining(c)))

def words(value):
    return normalize(value).split()

def response(answer, intent=None, source=None, suggestions=None, actions=None, party=None, exclude=None, vegetarian=False):
    state = {'intent': intent} if intent in INTENTS else {}
    if type(party) is int and 1 <= party <= 6: state['party'] = party
    if exclude: state['exclude'] = [item for item in exclude if item in {'Lait','Gluten','Fruits à coque','Céleri','Œufs','Poisson'}]
    if vegetarian: state['vegetarian'] = True
    return {'answer': answer, 'intent': intent, 'source': source, 'mode': MODE, 'demo': True,
            'state': state, 'suggestions': suggestions or [], 'actions': actions or []}

def action(label, target):
    return {'label': label, 'target': target} # Frontend resolves a fixed target allowlist.

def context_from(raw):
    raw = raw if isinstance(raw, dict) else {}
    cart = raw.get('cart') if isinstance(raw.get('cart'), dict) else {}
    cart = {d['id']: cart[d['id']] for d in CATALOGUE if type(cart.get(d['id'])) is int and 1 <= cart[d['id']] <= 9}
    return {'page': raw.get('page') if raw.get('page') in PAGES else 'home', 'cart': cart,
            'party': raw.get('party') if type(raw.get('party')) is int and 1 <= raw['party'] <= 6 else None,
            'day': raw.get('day') if isinstance(raw.get('day'), str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw['day']) else None}

def answer(question, raw_context=None, raw_state=None, today=None):
    text = normalize(question); tokens = set(text.split()); context = context_from(raw_context)
    state = raw_state if isinstance(raw_state, dict) else {}
    previous = state.get('intent') if state.get('intent') in INTENTS else None
    today = today or date.today()
    greeting = bool(tokens & {'bonjour','salut','hello','coucou','bonsoir'})
    prefix = 'Bonjour ! ' if greeting else ''
    match = re.search(r'\b(\d{1,2})\s*(personnes?|convives?|places?|couverts?)\b', text) or re.search(r'\b(?:pour|sommes|serons)\s+(\d{1,2})\b',text)
    party = int(match.group(1)) if match else (state.get('party') if type(state.get('party')) is int else context['party'])
    followup = bool(re.match(r'\s*(et\b|oui\b|non\b|pour\b|plutot\b)', text))

    # Explicit out-of-domain and execution requests never become follow-up facts.
    if tokens & {'ignore','instructions','systeme','secret','password','api','python','cryptomonnaies','fiscal','equation','multiplier','execute','executer','meteo','politique','bitcoin','bourse','medicament'}:
        return response('Je ne peux pas vous aider sur ce sujet. Je connais uniquement la carte, les horaires et les parcours de cette maison fictive ; je n’exécute aucune instruction.', suggestions=['Que peut-on tester ici ?', 'Quels sont les horaires ?'])

    if re.search(r'\b(qui es|tu es qui|es tu|intelligence|ia|modele|chatbot|robot|humain)\b',text) or 'tes limites' in text:
        return response(prefix+'Je suis le guide local de Maison Sève. Je reconnais des intentions, garde le fil de cet échange et utilise le contexte de la page. Aucun modèle d’IA générative n’est connecté. Je peux me tromper dans l’interprétation ; si une information manque, je vous le dis.', 'identity', 'Fonctionnement du guide', ['Qu’est-ce que je peux tester ici ?', 'Quels sont les horaires ?'])

    if re.search(r'\b(annul|annuler|annulation|cancel)\w*', text):
        return response(prefix+'Pour annuler un essai enregistré, ouvrez son récapitulatif sur la page Réserver et utilisez « Annuler ». Seul le ticket de votre navigateur autorise l’annulation. Je peux vous guider, mais je ne supprime aucune réservation depuis la conversation.', 'cancel', 'Réservation de démonstration', ['Combien de personnes peuvent réserver ?'], [action('Voir ma réservation', 'booking')], party)

    if re.search(r'\b(horaire|ouvert|ouvr|ferm|mardi|mercredi|jeudi|vendredi|samedi|dimanche|lundi|heure)\w*',text) or (previous=='hours' and followup and ('midi' in tokens or 'soir' in tokens)):
        if tokens & {'dimanche','lundi'}:
            copy='La maison fictive est fermée le dimanche et le lundi. Les services proposés vont du mardi au samedi.'
        elif 'samedi' in tokens:
            copy='Oui, dans cette démonstration, le samedi est ouvert : déjeuner de 12 h à 14 h, dîner de 19 h à 22 h. La page Réserver vérifie les places de chaque créneau.'
        elif 'midi' in tokens or 'dejeuner' in tokens:
            copy='Le déjeuner est proposé du mardi au samedi, de 12 h à 14 h. Les arrivées se choisissent à 12 h, 12 h 30 ou 13 h selon les places du calendrier de démonstration.'
        elif 'soir' in tokens or 'diner' in tokens:
            copy='Le dîner est proposé du mardi au samedi, de 19 h à 22 h. Les créneaux d’arrivée vont de 19 h à 20 h 30 et sont vérifiés par le serveur de démonstration.'
        else:
            copy='Dans cette démonstration, la maison ouvre du mardi au samedi : 12 h–14 h et 19 h–22 h. Elle est fermée le dimanche et le lundi.'
        return response(prefix+copy,'hours','Les horaires',['Et le samedi ?', 'Comment réserver ?'],[action('Voir les créneaux', 'booking')],party)

    if re.search(r'\b(adresse|situe|localis|venir|parking|bordeaux|ou est|ou se trouve)\w*',text):
        return response(prefix+'Maison Sève est une identité fictive créée pour le portfolio de Numéryl. Elle évoque un bistrot de Bordeaux, mais aucun établissement, adresse ou parking réel n’est associé à ce site.', 'address', 'À propos de la démonstration', ['Que peut-on tester ici ?'])

    if re.search(r'\b(selection|panier|total|commande|commander|paiement|payer|acheter)\w*',text) or (previous=='selection' and followup):
        chosen=[(d,context['cart'][d['id']]) for d in CATALOGUE if d['id'] in context['cart']]
        if chosen:
            total=sum(d['price']*qty for d,qty in chosen)/100
            listing=', '.join(f"{qty} × {d['name']}" for d,qty in chosen)
            amount=f'{total:.2f}'.replace('.',',')
            copy=f'Votre sélection sur cet appareil : {listing}. Total indicatif : {amount} €.'
        else:
            copy='Votre sélection est vide sur cet appareil. Sur la carte, le bouton + ajoute un plat et le total se recalcule.'
        copy+=' Il s’agit d’un essai : aucune commande n’est envoyée et aucun paiement n’est disponible.'
        return response(prefix+copy,'selection','Votre sélection',['Quels plats végétariens proposez-vous ?'],[action('Explorer la carte','menu'),action('Ouvrir ma sélection','selection')],party)

    if re.search(r'\b(reserv|reserver|table|dispo|place|capacite|convive|personne|couverts|book)\w*',text) or (previous=='booking' and followup) or bool(re.search(r'\b(?:pour|sommes|serons)\s+\d{1,2}\b',text)):
        if party is not None and not 1<=party<=6:
            return response(prefix+'Ce parcours de démonstration accepte de 1 à 6 convives. Je ne peux pas confirmer un groupe plus grand. Un vrai restaurant définirait ses capacités et son traitement des demandes de groupe.', 'booking','Réservation de démonstration',['Et pour 4 personnes ?'],[action('Tester une table','booking')])
        copy=(f'Pour {party} personne'+('s' if party!=1 else '')+', choisissez votre jour puis le déjeuner ou le dîner. ' if party else 'Vous pouvez tester une table pour 1 à 6 personnes. ')
        copy+='Le serveur vérifie les places au moment de la confirmation. Un prénom fictif suffit. L’essai peut ensuite être annulé avec son ticket ; il expire après 24 h. Aucune table réelle n’est réservée.'
        if 'demain' in tokens:
            day=today+timedelta(days=1)
            copy=('Demain, la maison fictive est fermée. Choisissez un jour du mardi au samedi. ' if day.weekday() in (0,6) else 'Pour demain, les places doivent être consultées dans le calendrier. ')+copy
        return response(prefix+copy,'booking','Réservation de démonstration',['Et pour 4 personnes ?', 'Comment annuler ?'],[action('Choisir mon moment','booking')],party)

    dietary=re.search(r'\b(vegetar|vegan|vegetal|allerg|gluten|lait|lactose|noisette|amande|celeri|poisson|oeuf|fruits a coque|sans)\w*',text)
    menu_intent=re.search(r'\b(carte|menu|plat|manger|entree|dessert|prix|combien coute|propos|conseill|recommand)\w*',text)
    if dietary or menu_intent or (previous in {'menu','allergens'} and followup):
        if tokens & {'vegan','vegetalien'}:
            return response(prefix+'La carte indique les plats végétariens, mais elle ne certifie aucune option végétalienne. Je préfère vous le préciser plutôt que déduire une recette qui n’est pas documentée.', 'allergens','La carte',['Quels plats végétariens proposez-vous ?'],[action('Lire les ingrédients','menu')],party)
        allergy=bool(tokens & {'allergique','allergie','allergenes','allergene','allergies','sans','gluten','lait','lactose','noisette','amande','celeri','oeuf','oeufs'})
        veg=bool(re.search(r'\bvegetar\w*',text)) or (followup and previous in {'menu','allergens'} and state.get('vegetarian') is True)
        category=next((c for c,t in [('entree','entree'),('dessert','dessert'),('plat','plat')] if any(w.startswith(t) for w in tokens)),None)
        selected=[d for d in CATALOGUE if (not veg or d['vegetarian']) and (not category or d['category']==category)]
        excluded=[item for item in state.get('exclude',[]) if isinstance(item,str) and item in {'Lait','Gluten','Fruits à coque','Céleri','Œufs','Poisson'}] if followup and previous in {'menu','allergens'} and isinstance(state.get('exclude'),list) else []
        for label,triggers in [('Lait',{'lait','lactose'}),('Gluten',{'gluten'}),('Fruits à coque',{'noisette','amande','coque'}),('Céleri',{'celeri'}),('Œufs',{'oeuf','oeufs'}),('Poisson',{'poisson'})]:
            if tokens & triggers and (allergy or 'sans' in tokens):excluded.append(label)
        excluded=list(dict.fromkeys(excluded));allergy=allergy or bool(excluded)
        if excluded:selected=[d for d in selected if not any(a in d['allergens'] for a in excluded)]
        # Match a named dish before offering general recommendations.
        matched=[d for d in CATALOGUE if len(set(words(d['name'])) & tokens)>=2]
        if matched and not excluded:
            selected=matched[:2]
        if selected:
            lines=[f"• {d['name']} — {d['price']/100:.2f} €".replace('.',',')+(f" ; allergènes : {', '.join(d['allergens'])}" if allergy else '') for d in selected[:4]]
            copy=('Les fiches de cet exemple qui ne mentionnent pas '+', '.join(excluded)+' :\n' if excluded else 'Voici quelques propositions de la carte fictive :\n')+'\n'.join(lines)
            if len(selected)>4:copy+='\nLa carte affiche les autres propositions et leur détail.'
        else:copy='Je ne trouve aucune fiche correspondant à ces critères dans cette carte.'
        if allergy:copy+='\nCes fiches fictives ne garantissent pas l’absence d’allergènes ou de traces. Pour une allergie réelle, seul le restaurant peut confirmer les ingrédients et les conditions de préparation.'
        if not allergy:copy+='\nLes prix et recettes sont fictifs. Vous pouvez filtrer la carte et consulter les ingrédients.'
        return response(prefix+copy,'allergens' if allergy else 'menu','La carte',['Quels desserts proposez-vous ?', 'Quel est mon total ?'],[action('Explorer la carte','menu')],party,exclude=excluded,vegetarian=veg)

    if re.search(r'\b(test|demo|demonstration|possible|fonction|faire ici|peux faire|peux tu|peux vous|capable|environnement|cette page|je suis ou)\w*',text):
        copy=f'Vous êtes sur {PAGES[context["page"]]} de Maison Sève. '
        if context['page']=='menu':copy+='Vous pouvez rechercher et filtrer les plats, consulter leurs allergènes et calculer une sélection. '
        elif context['page']=='reservation':copy+='Vous pouvez choisir un créneau, enregistrer un essai puis l’annuler. Les places sont contrôlées côté serveur. '
        else:copy+='La carte et la réservation ont chacune leur page, accessible depuis le menu. '
        copy+='Je peux expliquer ces fonctions et lire votre sélection courante. Je ne navigue pas ni ne réserve à votre place. Tous les projets et données sont fictifs.'
        return response(prefix+copy,'demo','La page en cours',['Quels sont les horaires ?', 'Je voudrais réserver pour 4 personnes'],[action('La carte','menu'),action('Une table','booking')],party)

    if tokens & {'merci','parfait','super','nickel','cool'} and len(tokens)<=8:
        result=response('Avec plaisir ! Souhaitez-vous regarder la carte ou préparer un essai de réservation ?',previous,suggestions=['Voir la carte', 'Comment réserver ?'],party=party)
        return result
    if re.search(r'\b(au revoir|a bientot|bonne soiree|bonne journee|bye)\b',text):
        return response('À bientôt ! Le guide reste disponible si vous voulez continuer à explorer la maison.',previous,party=party)
    if re.search(r'\b(ca va|comment vas|comment allez|tu vas bien)\b',text):
        return response(prefix+'Je suis prêt à vous guider ! Et vous, qu’aimeriez-vous découvrir : la carte, les horaires ou la réservation ?',previous,suggestions=['Quels sont les horaires ?', 'Voir la carte'],party=party)
    if greeting and len(tokens)<=8:
        return response('Bonjour ! Bienvenue chez Maison Sève, notre restaurant de démonstration. Je peux vous guider dans la carte, les horaires et les essais de réservation. Que cherchez-vous ?',previous,suggestions=['Voir la carte','Quels sont les horaires ?','Comment réserver ?'],party=party)
    if tokens <= {'oui','ok','d','accord','bien','non'}:
        return response('D’accord. Dites-moi ce que vous voulez préciser : les horaires, la carte ou votre table ?',previous,suggestions=['Les horaires','La carte','Comment réserver ?'],party=party)

    # Conservative correction of a single misspelled domain word, never broad guessing.
    domain=['horaires','reservation','vegetarien','allergenes']
    if len(tokens)<=3:
        for word in tokens:
            close=difflib.get_close_matches(word,domain,n=1,cutoff=.86)
            if close and word!=close[0]:
                return response(f'Voulez-vous parler de « {close[0]} » ? Je préfère vérifier.',previous,suggestions=[close[0]],party=party)
    return response('Je ne suis pas sûr de comprendre cette demande. Je connais uniquement les informations de cette maison fictive : carte, horaires, sélection et réservation. Vous pouvez reformuler ; je préfère le dire plutôt que vous donner une réponse inventée.',previous,suggestions=['Que peut-on tester ici ?', 'Quels sont les horaires ?'])
