(() => {
  "use strict";

  const GENRES = ["Fantasy", "Romance", "Mystery", "Thriller", "Sci-Fi", "Horror", "Classics", "Literary fiction", "Historical", "Adventure", "Nonfiction", "Spiritual"];
  const MORE_GENRES = ["Biography", "Crime", "Contemporary", "Dystopian", "Family", "Humor", "Poetry", "Short stories", "War", "Young adult"];
  const MOODS = ["Joyful", "Playful", "Hopeful", "Cozy", "Curious", "Adventurous", "Thoughtful", "Spiritual", "A little thrill", "Ready to cry"];
  const MORE_MOODS = ["Calm", "Inspired", "Nostalgic", "Romantic", "Courageous", "Reflective", "Surprised", "Escapist", "Motivated", "Mysterious"];
  const MAX_SELECTED = 5;

  function element(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }

  function makePanelLabel(kicker, title, description) {
    const heading = element("div", "recommendation-card-heading");
    heading.append(element("span", "recommendation-card-kicker", kicker));
    heading.append(element("h2", "recommendation-card-title", title));
    heading.append(element("p", "recommendation-card-description", description));
    return heading;
  }

  function addTextEntry(container, { id, label, placeholder, hint }, onChange) {
    const card = element("section", "recommendation-card recommendation-entry-card");
    const heading = makePanelLabel(label.kicker, label.title, label.description);
    const form = element("form", "recommendation-entry-form");
    form.autocomplete = "off";
    const input = element("input", "recommendation-entry-input");
    input.id = id;
    input.type = "text";
    input.maxLength = 160;
    input.placeholder = placeholder;
    input.setAttribute("aria-label", label.title);
    const addButton = element("button", "recommendation-add-button", "Add");
    addButton.type = "submit";
    form.append(input, addButton);
    const chips = element("div", "recommendation-entry-chips");
    chips.setAttribute("aria-live", "polite");
    const help = element("p", "recommendation-card-hint", hint);
    const values = new Set();

    function renderChips() {
      chips.replaceChildren();
      values.forEach((value) => {
        const chip = element("span", "recommendation-value-chip");
        chip.append(element("span", "recommendation-value-text", value));
        const remove = element("button", "recommendation-chip-remove", "×");
        remove.type = "button";
        remove.setAttribute("aria-label", `Remove ${value}`);
        remove.addEventListener("click", () => {
          values.delete(value);
          renderChips();
          onChange();
        });
        chip.append(remove);
        chips.append(chip);
      });
    }

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const value = input.value.trim().replace(/\s+/g, " ");
      if (!value) return;
      values.add(value);
      input.value = "";
      renderChips();
      onChange();
      input.focus();
    });
    card.append(heading, form, chips, help);
    container.append(card);
    return values;
  }

  function addMultiSelect(container, items, extraItems, className, labelPrefix, onChange) {
    const selected = new Set();
    const choices = element("div", className);
    const extraChoices = element("div", `${className}-extra`);
    extraChoices.hidden = true;
    const summary = element("p", `${className}-summary`);

    function refreshSelection() {
      [...choices.querySelectorAll("button"), ...extraChoices.querySelectorAll("button")].forEach((button) => {
        const isSelected = selected.has(button.textContent);
        button.classList.toggle("selected", isSelected);
        button.setAttribute("aria-pressed", String(isSelected));
      });
      const count = selected.size;
      summary.textContent = count ? `${count} ${labelPrefix}${count === 1 ? "" : "s"} picked` : "";
    }

    function addChoice(item, target) {
      const button = element("button", `${className}-choice`, item);
      button.type = "button";
      button.setAttribute("aria-pressed", "false");
      button.addEventListener("click", () => {
        if (selected.has(item)) {
          selected.delete(item);
          button.classList.remove("selected");
          button.setAttribute("aria-pressed", "false");
        } else {
          if (selected.size >= MAX_SELECTED) return;
          selected.add(item);
          button.classList.add("selected");
          button.setAttribute("aria-pressed", "true");
        }
        refreshSelection();
        onChange();
      });
      target.append(button);
    }

    items.forEach((item) => addChoice(item, choices));
    extraItems.forEach((item) => addChoice(item, extraChoices));
    const toggle = element("button", `${className}-toggle`, `Show more ${labelPrefix}s`);
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", () => {
      const expanded = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", String(expanded));
      extraChoices.hidden = !expanded;
      toggle.textContent = `${expanded ? "Show fewer" : "Show more"} ${labelPrefix}s`;
    });
    if (extraItems.length) container.append(choices, extraChoices, toggle, summary);
    else container.append(choices, summary);
    return selected;
  }

  function mount(container, requestApi, onBookSelect) {
    if (!container || container.dataset.mounted === "true") return;
    container.dataset.mounted = "true";

    const hero = element("header", "recommendations-hero");
    hero.append(element("p", "recommendations-hero-kicker", "A LITTLE MAGIC, JUST FOR YOU"));
    const title = element("h1", "recommendations-title", "Tell Annie what you love…");
    title.id = "recommendations-title";
    hero.append(title);
    hero.append(element("p", "recommendations-intro", "and she’ll uncover the stories waiting for you."));
    const quote = element("blockquote", "recommendations-quote");
    quote.append(element("p", "recommendations-quote-text", "“A reader lives a thousand lives before he dies... The man who never reads lives only one.”"));
    hero.append(quote);
    container.append(hero);

    const results = element("section", "recommendation-results");
    results.hidden = true;
    results.setAttribute("aria-live", "polite");
    const resultHeading = element("div", "recommendation-results-heading");
    resultHeading.append(element("p", "recommendation-card-kicker", "CURATED FOR YOUR TASTE"));
    resultHeading.append(element("h2", "recommendation-card-title", "Your next reads"));
    const resultMessage = element("p", "recommendation-results-message");
    const resultList = element("div", "recommendation-results-list");
    const loadMore = element("button", "recommendation-load-more", "Load more recommendations");
    loadMore.type = "button";
    loadMore.hidden = true;
    const shelfGuide = element("aside", "recommendation-shelf-guide");
    shelfGuide.append(element("p", "recommendation-card-kicker", "KEEP EXPLORING"));
    shelfGuide.append(element("h3", "recommendation-shelf-guide-title", "A different clue can open a new shelf"));
    shelfGuide.append(element("p", "recommendation-shelf-guide-copy", "Change a genre, mood, or book clue and Annie will look in a new direction."));
    const guideClues = element("div", "recommendation-shelf-guide-clues");
    const guideButton = element("button", "recommendation-shelf-guide-button", "Adjust my picks");
    guideButton.type = "button";
    shelfGuide.append(guideClues, guideButton);
    const sourceCredit = element("p", "recommendation-source-credit");
    const sourceLink = element("a", "recommendation-source-link", "Some discovery results via Big Book API");
    sourceLink.href = "https://bigbookapi.com/";
    sourceLink.target = "_blank";
    sourceLink.rel = "noopener noreferrer";
    sourceCredit.append(sourceLink);
    sourceCredit.hidden = true;
    results.append(resultHeading, resultMessage, resultList, loadMore, shelfGuide, sourceCredit);
    let shownBooks = [];
    let currentPreferences = null;

    function clearStaleResults() {
      results.hidden = true;
      resultMessage.textContent = "";
      sourceCredit.hidden = true;
      loadMore.hidden = true;
      shownBooks = [];
      currentPreferences = null;
    }

    guideButton.addEventListener("click", () => {
      container.querySelector(".recommendation-inputs")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });

    function renderGuideClues(preferences) {
      guideClues.replaceChildren();
      const clues = [
        ...preferences.genres.map((value) => `Genre · ${value}`),
        ...preferences.moods.map((value) => `Mood · ${value}`),
      ];
      const seedCount = preferences.read.length + preferences.liked.length;
      if (seedCount) clues.unshift(`${seedCount} book or author ${seedCount === 1 ? "clue" : "clues"}`);
      clues.slice(0, 6).forEach((clue) => guideClues.append(element("span", "recommendation-shelf-guide-chip", clue)));
    }

    loadMore.addEventListener("click", async () => {
      if (!currentPreferences || !shownBooks.length || typeof requestApi !== "function") return;
      const requestPreferences = currentPreferences;
      loadMore.disabled = true;
      loadMore.textContent = "Finding fresh matches…";
      resultMessage.textContent = "Annie is looking for more books that haven’t appeared yet.";
      try {
        const response = await requestApi("recommendations", {
          method: "POST",
          body: JSON.stringify({
            ...requestPreferences,
            exclude_books: shownBooks.map((book) => ({ title: book.title, author: book.author })),
          }),
        });
        if (currentPreferences !== requestPreferences) return;
        const moreBooks = Array.isArray(response.data?.books) ? response.data.books : [];
        const seen = new Set(shownBooks.map((book) => `${String(book.title || "").toLocaleLowerCase()}|${String(book.author || "").toLocaleLowerCase()}`));
        const freshBooks = moreBooks.filter((book) => {
          const key = `${String(book.title || "").toLocaleLowerCase()}|${String(book.author || "").toLocaleLowerCase()}`;
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        });
        if (!freshBooks.length) {
          resultMessage.textContent = "Annie has found all the strong matches for these preferences. Try adding another book, genre, or mood to explore further.";
          loadMore.hidden = true;
          return;
        }
        shownBooks.push(...freshBooks);
        freshBooks.forEach((book) => resultList.append(renderRecommendation(book)));
        resultMessage.textContent = `${shownBooks.length} picks, balanced for fit and reader quality.`;
        const sourcesUsed = Array.isArray(response.data?.sources_used) ? response.data.sources_used : [];
        if (sourcesUsed.includes("bigbookapi") || freshBooks.some((book) => book.source === "bigbookapi")) {
          sourceCredit.hidden = false;
        }
      } catch (error) {
        if (currentPreferences !== requestPreferences) return;
        resultMessage.textContent = error.message === "rate_limited"
          ? "Give Annie a moment, then try loading more."
          : "Annie couldn’t load more picks right now. Please try again.";
      } finally {
        loadMore.disabled = false;
        loadMore.textContent = "Load more recommendations";
      }
    });

    const bookSection = element("section", "recommendation-inputs", "");
    bookSection.setAttribute("aria-label", "Books that shape your recommendations");
    const readEntries = addTextEntry(bookSection, {
      id: "recommendation-read-input",
      label: { kicker: "YOUR READING TRAIL", title: "What have you read?", description: "A title or author gives Annie a place to begin." },
      placeholder: "Add a book or author…",
      hint: "Press Add or Enter after each title or author.",
    }, clearStaleResults);
    const likedEntries = addTextEntry(bookSection, {
      id: "recommendation-loved-input",
      label: { kicker: "THE ONES THAT STAYED", title: "What did you love?", description: "Tell us about the books you’d happily revisit." },
      placeholder: "Add a favorite book or author…",
      hint: "A favorite can say as much as a whole reading list.",
    }, clearStaleResults);
    container.append(bookSection);

    const genreCard = element("section", "recommendation-card recommendation-genre-card");
    genreCard.append(makePanelLabel("SET THE SCENE", "Choose your genres", "Pick the kinds of stories you’d like to find more often."));
    const genreHint = element("p", "recommendation-card-hint", "Genres can refine title or author clues; starting with one is a good place if you haven’t added either yet.");
    genreCard.append(genreHint);
    const selectedGenres = addMultiSelect(genreCard, GENRES, MORE_GENRES, "recommendation-genres", "genre", clearStaleResults);
    container.append(genreCard);

    const moodCard = element("section", "recommendation-card recommendation-mood-card");
    const moodHeader = element("div", "recommendation-mood-header");
    moodHeader.append(element("span", "recommendation-mood-spark", "✦"));
    const moodCopy = element("div", "recommendation-mood-copy");
    moodCopy.append(element("p", "recommendation-card-kicker", "LET YOUR MOOD DECIDE"));
    moodCopy.append(element("h2", "recommendation-card-title", "Not sure what you’re looking for?"));
    moodCopy.append(element("p", "recommendation-card-description", "How are you feeling today? Pick a mood and let it lead you to a story."));
    moodHeader.append(moodCopy);
    moodCard.append(moodHeader);
    const selectedMoods = addMultiSelect(moodCard, MOODS, MORE_MOODS, "recommendation-moods", "mood", clearStaleResults);
    container.append(moodCard);

    const submit = element("button", "recommendation-submit", "Find my next reads");
    submit.type = "button";
    const quickStart = element("section", "recommendation-quickstart");
    quickStart.append(element("span", "recommendation-card-kicker", "ONE CLUE IS ENOUGH"));
    quickStart.append(element("h2", "recommendation-quickstart-title", "Where would you like to begin?"));
    quickStart.append(element("p", "recommendation-quickstart-copy", "Jump straight to the clue you have in mind."));
    const quickStartActions = element("div", "recommendation-quickstart-actions");
    [
      { label: "Add a book or author", target: bookSection, focus: "#recommendation-read-input", icon: "▤" },
      { label: "Choose a genre", target: genreCard, focus: ".recommendation-genres-choice", icon: "◈" },
      { label: "Pick a mood", target: moodCard, focus: ".recommendation-moods-choice", icon: "✦" },
    ].forEach(({ label, target, focus, icon }) => {
      const action = element("button", "recommendation-quickstart-action");
      action.type = "button";
      action.append(element("span", "recommendation-quickstart-icon", icon));
      action.append(element("span", "recommendation-quickstart-label", label));
      action.addEventListener("click", () => {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        target.querySelector(focus)?.focus({ preventScroll: true });
      });
      quickStartActions.append(action);
    });
    quickStart.append(quickStartActions);
    const submitStatus = element("p", "recommendation-submit-status");
    submitStatus.setAttribute("role", "status");
    submitStatus.setAttribute("aria-live", "polite");
    container.append(submit, submitStatus, quickStart, results);

    submit.addEventListener("click", async () => {
        const preferences = {
        read: [...readEntries],
        liked: [...likedEntries],
        genres: [...selectedGenres],
        moods: [...selectedMoods],
        };
      if (!preferences.read.length && !preferences.liked.length && !preferences.genres.length && !preferences.moods.length) {
        submitStatus.textContent = "Add a book or author, choose a genre, or pick a mood to begin.";
        return;
      }
      if (typeof requestApi !== "function") {
        submitStatus.textContent = "Recommendations are unavailable right now. Please try again.";
        return;
      }

      submit.disabled = true;
      submit.textContent = "Finding thoughtful matches…";
        submitStatus.textContent = "Annie is looking beyond the bestsellers for books that fit you.";
        results.hidden = true;
        currentPreferences = preferences;
        renderGuideClues(preferences);
        try {
        const response = await requestApi("recommendations", {
          method: "POST",
          body: JSON.stringify(preferences),
        });
        const books = Array.isArray(response.data?.books) ? response.data.books : [];
        const sourcesUsed = Array.isArray(response.data?.sources_used) ? response.data.sources_used : [];
        sourceCredit.hidden = !sourcesUsed.includes("bigbookapi") && !books.some((book) => book.source === "bigbookapi");
        resultList.replaceChildren();
        shownBooks = books.slice();
        loadMore.hidden = !books.length;
        if (!books.length) {
          resultMessage.textContent = "Annie couldn’t find enough strong matches yet. Try another book, genre, or mood.";
        } else {
          resultMessage.textContent = `${books.length} picks, balanced for fit and reader quality.`;
          books.forEach((book) => resultList.append(renderRecommendation(book)));
        }
        results.hidden = false;
        submitStatus.textContent = "";
        results.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (error) {
        submitStatus.textContent = error.message === "rate_limited"
          ? "Give Annie a moment, then try again."
          : error.message === "recommendation_input_required"
            ? "Add a book or author, choose a genre, or pick a mood to begin."
            : "Recommendations couldn’t be loaded right now. Please try again.";
      } finally {
        submit.disabled = false;
        submit.textContent = "Find my next reads";
      }
    });

    function renderRecommendation(book) {
      const card = element("button", "recommendation-result-card");
      card.type = "button";
      card.setAttribute("aria-label", `View full details for ${book.title || "recommended book"}`);
      card.addEventListener("click", () => {
        if (typeof onBookSelect === "function") onBookSelect(book);
      });
      const coverUrl = safeUrl(book.cover_url);
      if (coverUrl) {
        const cover = element("img", "recommendation-result-cover");
        cover.src = coverUrl;
        cover.alt = `Cover of ${book.title || "recommended book"}`;
        cover.loading = "lazy";
        cover.decoding = "async";
        cover.addEventListener("error", () => cover.replaceWith(element("div", "recommendation-result-cover recommendation-result-cover-empty", "✧")), { once: true });
        card.append(cover);
      } else {
        card.append(element("div", "recommendation-result-cover recommendation-result-cover-empty", "✧"));
      }
      const copy = element("div", "recommendation-result-copy");
      copy.append(element("h3", "recommendation-result-title", book.title || "Untitled"));
      copy.append(element("p", "recommendation-result-author", book.author || "Unknown author"));
      const rating = Number(book.rating || 0);
      const count = Number(book.rating_count || 0);
      copy.append(element("p", "recommendation-result-rating", rating > 0
        ? `★ ${rating.toFixed(2)}${count > 0 ? ` · ${count.toLocaleString()} ratings` : ""}`
        : "Ratings not yet available"));
      if (book.recommendation_reason) copy.append(element("p", "recommendation-result-reason", book.recommendation_reason));
      card.append(copy);
      return card;
    }
  }

  function safeUrl(value) {
    if (typeof value !== "string" || !value) return "";
    try {
      const url = new URL(value, window.location.href);
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
    } catch (_) { return ""; }
  }

  window.AnnieRecommendations = Object.freeze({ mount });
})();
