---
name: flashcards
description: Build a flashcard study app. Use when asked to create flashcards, vocabulary cards, study app, memorization tool, language learning cards, or any flip-card learning experience.
---

# Flashcards Template

Build flip-card study apps from this template. Works for vocabulary, language learning, history facts, science terms — any topic with front/back pairs.

## How to use

1. Read the template: `assets/index.html`
2. Copy it to the user's project directory with `write_file`
3. Customize these parts:
   - Replace `{{TITLE}}` with the app name (e.g. "French Vocabulary")
   - Replace `{{ICON}}` with an emoji (e.g. 🇫🇷, 🧠, 📚)
   - Replace `{{SUBTITLE}}` with a tagline
   - Replace the `CARDS` array with your content:
     ```js
     const CARDS = [
       { front: "Question", back: "Answer", emoji: "📝", detail: "Extra info" },
     ];
     ```

## Built-in features (don't rebuild these)

- 3D flip card animation (CSS transform)
- Know it / Again buttons (spaced repetition)
- Progress bar and score tracking
- Cards shuffle on restart
- "Again" cards go back in the deck
- End screen with final score
- Mobile responsive

## Testing

After creating the app, verify:
1. Card displays front text
2. Tapping flips to show back
3. Know/Again buttons appear after flip
4. Progress bar advances
5. End screen shows when deck is empty
