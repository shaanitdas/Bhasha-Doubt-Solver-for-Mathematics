# Bhasha Doubt Solver

Bhasha is an interactive math tutor designed to help students understand their own reasoning. Instead of immediately giving an answer, it checks the student’s work, identifies supported misconception patterns, and guides the next step.

## Student experience

- Students submit a math question and, for guided help, explain what they have tried.
- **Guide me** responds with a Socratic prompt and adapts later hints to the current problem and the learner’s misconception history.
- **Show answer** requests a complete worked solution and a similar practice problem. It can be used without entering an attempt.
- Students can choose English, Hindi, Hinglish, or Bengali feedback and record how confident they feel.
- The Misconception Map summarizes recurring patterns, helping students see what they are working on over time.

## Learner history

The learner model tracks misconception counts and hint progression. It is stored in the browser’s local storage for that browser profile and device, so it persists across visits but does not sync between devices. Students can reset it from the app.

## How it is built

- `public/index.html` contains the responsive interface and browser-side learner history.
- `main.py` validates requests, calls Sarvam for independent checking and tutoring, and manages local SQLite data.
- `api/index.py` exposes the Python request handler to the serverless runtime.
