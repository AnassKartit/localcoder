---
name: quiz-game
description: Build an interactive quiz game app. Use when asked to create a quiz, trivia, math game, learning game, educational game, or any multiple-choice question app. Supports SVG visuals, score tracking, confetti, and sound effects.
---

# Quiz Game Template

Build interactive quiz apps from this template. Works for math, trivia, vocabulary, science — any topic with questions and answers.

## How to use

1. Read the template: `assets/index.html`
2. Copy it to the user's project directory with `write_file`
3. Customize these parts:
   - Replace `{{TITLE}}` with the app name (e.g. "Math Fractions Quiz")
   - Replace `{{ICON}}` with an emoji (e.g. 🍕, 🧮, 🌍)
   - Replace `{{SUBTITLE}}` with a tagline
   - Replace `{{QUESTION_TEXT}}` with the question prompt
   - Modify the `generateQuestion()` function for your topic
   - Modify the `drawPie()` function or replace with different SVG visuals
   - Update CSS colors in `:root` to match the theme

## Built-in features (don't rebuild these)

- Start screen → Game screen → End screen transitions
- SVG pie chart with real Math.cos/Math.sin arc paths
- 4 answer buttons with correct/wrong animations
- Score counter with ⭐ stars
- Progress bar
- Confetti celebration on correct answer
- Web Audio API sound effects
- Mobile responsive

## Example customizations

**Math fractions:** Keep the SVG pie, change questions to fractions
**World capitals:** Remove SVG, show country flag emoji, change to geography questions
**Vocabulary:** Remove SVG, show word in large text, answers are definitions
**Times tables:** Remove SVG, show "7 × 8 = ?", answers are numbers

## Testing

After creating the app, verify:
1. PLAY button hides start screen and shows game
2. SVG visual renders correctly (if using pie chart)
3. Clicking an answer shows correct/wrong feedback
4. Score increments on correct answers
5. End screen shows after all questions
