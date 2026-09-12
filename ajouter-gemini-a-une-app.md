# Requête à copier pour ajouter Gemini à une de mes apps

> Remplacer ce qui est entre crochets, supprimer les sections sans objet, joindre le fichier.

---

Je te joins **[nom-du-fichier.html]**, une de mes apps HTML monofichier, hébergée sur GitHub Pages. Elle interroge aujourd'hui **ChatGPT et Claude**, et je veux y ajouter **Gemini comme troisième choix**.

## Ce qui change avec Gemini

ChatGPT et Claude sont interrogés en ouvrant un onglet avec la question pré-remplie ; je copie la réponse à la main et je la recolle dans l'app. **Gemini s'interroge par son API depuis la page elle-même : la réponse revient directement dans l'app, sans presse-papier ni onglet.** Quand une photo est concernée, elle part dans la requête — ce que ni ChatGPT ni Claude ne permettent depuis une page web.

Conséquence : quand Gemini est sélectionné, tous les éléments du parcours presse-papier (boutons « Appliquer la réponse… », « Créer… à partir de la réponse copiée », fenêtres intermédiaires de copie, notes explicatives correspondantes) doivent **disparaître**. Le plus robuste est une classe posée sur `<body>` plutôt qu'un `style.display` dans chaque cas, car ces boutons ont souvent déjà leurs propres règles d'affichage :

```css
body.ia-gemini .presse-papier-seul{display:none !important;}
body:not(.ia-gemini) .gemini-seul{display:none !important;}
```

**ChatGPT et Claude doivent continuer à fonctionner exactement comme aujourd'hui, sans une ligne modifiée dans leur parcours.**

## L'appel à l'API (code qui fonctionne chez moi)

Endpoint, avec la clé en paramètre d'URL :

```
https://generativelanguage.googleapis.com/v1beta/models/<modele>:generateContent?key=<cle>
```

Modèles à proposer : `gemini-2.5-flash` (défaut, rapide), `gemini-2.5-pro` (plus fin, plus lent), `gemini-2.0-flash`.

```js
async function appelerGemini(question, dataUrlImage){
  if(!cleGemini) throw new Error("Aucune clé API Gemini sur cet appareil");
  const parts = [];
  if(question) parts.push({ text: question });
  if(dataUrlImage){
    const decoupe = /^data:([^;]+);base64,(.*)$/.exec(dataUrlImage);
    if(!decoupe) throw new Error("Photo au format inattendu");
    // Gemini n'accepte que des types image/* : un type perdu en route est corrigé ici.
    let typeImage = decoupe[1];
    if(!typeImage.startsWith("image/")) typeImage = "image/jpeg";
    parts.push({ inline_data: { mime_type: typeImage, data: decoupe[2] } });
  }
  const reponse = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${modeleGemini}:generateContent?key=${encodeURIComponent(cleGemini)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ contents: [{ parts }] })
  });
  let donnees = null;
  try{ donnees = await reponse.json(); }catch(e){ donnees = null; }
  if(!reponse.ok){
    throw new Error(donnees?.error?.message || ("HTTP " + reponse.status));
  }
  let texte = "";
  try{ texte = (donnees.candidates[0].content.parts || []).map(p => p.text || "").join("").trim(); }
  catch(e){ texte = ""; }
  if(!texte) throw new Error("Réponse vide — " + JSON.stringify(donnees).slice(0, 300));
  return texte;
}
```

## Réglages à ajouter

Dans la page Paramètres, à côté des choix ChatGPT / Claude, un troisième choix Gemini, et **visibles uniquement quand Gemini est sélectionné** : le champ de la clé API (`type="password"` avec un bouton 👁) et le choix du modèle.

- **La clé va dans le localStorage seul, jamais dans le fichier de réglages synchronisé sur GitHub** — elle y serait publiée dans le dépôt. Le modèle, lui, n'a rien de sensible et peut être synchronisé comme les autres réglages.
- Toutes mes apps sont servies depuis la même origine (`hammerinhank.github.io`), donc elles partagent le même localStorage : **utilise la clé de stockage `gemini_api_key`**, pour que la clé saisie dans une app serve à toutes les autres (c'est déjà le nom utilisé par geminiPhoto et vins.html). Si l'app rangeait déjà la clé sous un autre nom, reprends-la automatiquement au premier lancement sous le nom commun, puis efface l'ancienne entrée.
- Reste à saisir une fois par appareil (iMac, iPhone), le localStorage étant propre à chaque navigateur.
- Si le choix d'assistant est validé par un bouton Enregistrer, pense à réaligner l'interface quand la page est fermée sans enregistrer.

## Cas 1 — question en texte simple

La question est déjà composée par l'app pour ChatGPT/Claude : reprends-la telle quelle, appelle `appelerGemini(question, null)`, et **injecte la réponse là où arrivait jusqu'ici le texte collé depuis le presse-papier**, en passant par la même fenêtre de vérification s'il y en a une. Rien ne doit être enregistré sans ma confirmation, comme avant.

## Cas 2 — question avec photo jointe

Gemini attend l'image en base64 dans la même requête. Points à respecter :

- La source est un `data:` URL (`data:image/jpeg;base64,…`) : découpe-le, le type MIME et les données partent séparément.
- **Piège rencontré :** une photo récupérée via l'API GitHub arrive avec le type MIME de la réponse d'API (`application/vnd.github.raw+json`) et non celui de l'image. Un `<img>` s'en moque, Gemini refuse avec « Unsupported MIME type ». Rétablis le type réel à la récupération (d'après l'extension du fichier) et garde le garde-fou ci-dessus.
- Compresse avant l'envoi si l'app ne le fait pas déjà (1600 px, JPEG qualité 0,85) : une photo d'iPhone brute pèse plusieurs Mo, et le HEIC n'est pas accepté — un passage par un canvas règle les deux.
- Journalise le type et le poids de l'image envoyée.

## Retour visuel et diagnostic

Pendant l'attente (quelques secondes, parfois davantage avec `pro`) : bouton désactivé et libellé changé en « ⏳ … », plus un message court. En cas d'échec, affiche la cause exacte renvoyée par Google, pas un message générique.

Si l'app a un journal interne, trace : modèle appelé, longueur de la question, présence et poids de l'image, durée de la réponse, début du texte reçu, champs extraits, cause d'un refus.

Erreurs à anticiper dans les messages :

- `Requests from referer <empty> are blocked` ou mention de *referer* → la clé est restreinte par référent HTTP dans la console Google ; il faut y autoriser l'adresse de la page, ou lever la restriction.
- `API key not valid` → clé absente ou erronée sur cet appareil.
- Réponse vide ou sans les rubriques attendues → **n'ouvre pas** de fenêtre de vérification vide, qui effacerait des champs existants une fois confirmée ; affiche le début de la réponse reçue pour comprendre.

## Mes règles habituelles

- Incrémente le numéro de version affiché et **ajoute une entrée à l'historique** consultable en cliquant dessus, en quelques lignes.
- **Rends-moi le fichier complet**, pas des morceaux à insérer.
- Si un traitement boucle sur un lot d'éléments, affiche un décompte (traités / restants).
