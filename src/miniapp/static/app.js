(() => {
  "use strict";

  const webApp = window.Telegram?.WebApp;
  const elements = {
    form: document.querySelector("#search-form"),
    input: document.querySelector("#query"),
    searchButton: document.querySelector("#search-button"),
    startupMessage: document.querySelector("#startup-message"),
    typingIndicator: document.querySelector("#typing-indicator"),
    discover: document.querySelector("#discover-section"),
    genreTabs: document.querySelector("#genre-tabs"),
    discoverTitle: document.querySelector("#discover-title"),
    discoverNote: document.querySelector(".discover-note"),
    discoverMessage: document.querySelector("#discover-message"),
    trendingBooks: document.querySelector("#trending-books"),
    moreSection: document.querySelector("#more-section"),
    moreTrendingBooks: document.querySelector("#more-trending-books"),
    welcome: document.querySelector("#welcome"),
    resultsSection: document.querySelector("#results-section"),
    resultsTitle: document.querySelector("#results-title"),
    resultCount: document.querySelector("#result-count"),
    results: document.querySelector("#results"),
    message: document.querySelector("#message"),
    pagination: document.querySelector("#pagination"),
    previous: document.querySelector("#previous-page"),
    next: document.querySelector("#next-page"),
    pageLabel: document.querySelector("#page-label"),
    greeting: document.querySelector("#greeting"),
    dialog: document.querySelector("#book-dialog"),
    detail: document.querySelector("#book-detail"),
    closeDialog: document.querySelector("#close-dialog"),
    homeButton: document.querySelector("#home-button"),
    detailSearchForm: document.querySelector("#detail-search-form"),
    detailSearchInput: document.querySelector("#detail-query"),
    homeSticky: document.querySelector("#home-sticky"),
    recommendations: document.querySelector("#recommendations-view"),
    quickNav: document.querySelector("#quick-nav"),
    quickNavToggle: document.querySelector("#quick-nav-toggle"),
    quickMenuItems: document.querySelector("#quick-menu-items"),
    quickMenuBackdrop: document.querySelector("#quick-menu-backdrop"),
    themeButtons: Array.from(document.querySelectorAll("[data-theme-choice]")),
  };

  const state = {
    initData: "", query: "", page: 1, pageSize: 5, total: 0,
    hasMore: false, ready: false, requestId: 0, activeController: null,
    detailsController: null, detailsRequestId: 0, translationController: null,
    translationRequestId: 0, relatedController: null, relatedRequestId: 0,
    currentDetailBook: null, activeRelatedBooks: [], relatedLoading: false,
    relatedMessage: "", detailLoadingMessage: "", visibleBooks: [], selectedIndex: null,
    debounceTimer: null, activeDetailContext: null, featuredQuery: "",
    featuredBooks: [], featuredGenre: "All", featuredRequestId: 0, featuredCache: {},
    currentPage: "home", homeScrollY: 0,
  };

  const launchWelcome = document.querySelector("#launch-welcome");
  const launchWelcomeStartedAt = performance.now();
  let launchWelcomeDismissed = false;

  function dismissLaunchWelcome() {
    if (!launchWelcome || launchWelcomeDismissed) return;
    launchWelcomeDismissed = true;
    const remainingWelcomeTime = Math.max(0, 850 - (performance.now() - launchWelcomeStartedAt));
    window.setTimeout(() => {
      launchWelcome.classList.add("is-leaving");
      window.setTimeout(() => { launchWelcome.hidden = true; }, 400);
    }, remainingWelcomeTime);
  }

  window.setTimeout(dismissLaunchWelcome, 7000);

  const THEME_STORAGE_KEY = "annie-search-theme";
  const HOME_SHELF_SIZE = 10;
  const HOME_TRENDING_CACHE_MS = 60 * 60 * 1000;
  const THEME_COLORS = { purple: "#130e1b", light: "#eeeeef", amoled: "#000000" };

  function applyTheme(theme, { persist = true } = {}) {
    const selected = Object.prototype.hasOwnProperty.call(THEME_COLORS, theme) ? theme : "purple";
    document.documentElement.dataset.theme = selected;
    elements.themeButtons.forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.themeChoice === selected));
    });
    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) themeColor.content = THEME_COLORS[selected];
    if (persist) {
      try { window.localStorage.setItem(THEME_STORAGE_KEY, selected); } catch (_) { /* Storage may be disabled in embedded browsers. */ }
    }
    try { webApp?.setHeaderColor?.(THEME_COLORS[selected]); } catch (_) { /* Older Telegram clients. */ }
    try { webApp?.setBackgroundColor?.(THEME_COLORS[selected]); } catch (_) { /* Older Telegram clients. */ }
  }

  let savedTheme = "purple";
  try { savedTheme = window.localStorage.getItem(THEME_STORAGE_KEY) || savedTheme; } catch (_) { /* Use the default theme when storage is unavailable. */ }
  applyTheme(savedTheme, { persist: false });
  elements.themeButtons.forEach((button) => {
    button.addEventListener("click", () => applyTheme(button.dataset.themeChoice));
  });

  window.AnnieRecommendations?.mount(elements.recommendations, api, (book) => {
    openDetails(book, { collection: "recommendations", book: { ...book } });
  });

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function addDesktopScrollControls(scroller, label) {
    const wrapper = node("div", "horizontal-shelf");
    const previous = node("button", "shelf-scroll-button previous", "‹");
    previous.type = "button";
    previous.setAttribute("aria-label", `Scroll ${label} left`);
    const next = node("button", "shelf-scroll-button next", "›");
    next.type = "button";
    next.setAttribute("aria-label", `Scroll ${label} right`);
    const update = () => {
      wrapper.classList.toggle("can-scroll", scroller.scrollWidth > scroller.clientWidth + 2);
      previous.disabled = scroller.scrollLeft <= 2;
      next.disabled = scroller.scrollLeft + scroller.clientWidth >= scroller.scrollWidth - 2;
    };
    previous.addEventListener("click", () => {
      scroller.scrollBy({ left: -Math.max(180, scroller.clientWidth * 0.72), behavior: "smooth" });
    });
    next.addEventListener("click", () => {
      scroller.scrollBy({ left: Math.max(180, scroller.clientWidth * 0.72), behavior: "smooth" });
    });
    scroller.addEventListener("scroll", update, { passive: true });
    scroller.updateDesktopScrollControls = update;
    scroller.replaceWith(wrapper);
    wrapper.append(previous, scroller, next);
    window.requestAnimationFrame(update);
    return wrapper;
  }

  function refreshDesktopScrollControls(scroller) {
    window.requestAnimationFrame(() => scroller.updateDesktopScrollControls?.());
  }

  function updateShelfInstruction() {
    if (elements.discoverNote) {
      elements.discoverNote.textContent = window.matchMedia("(min-width: 700px)").matches
        ? "Use arrows to explore" : "Swipe to explore";
    }
  }

  function safeHttpUrl(value) {
    if (typeof value !== "string" || !value) return "";
    try {
      const url = new URL(value, window.location.href);
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : "";
    } catch (_) {
      return "";
    }
  }

  function appendSafeDescription(target, value) {
    const parsed = new DOMParser().parseFromString(String(value || ""), "text/html");
    const allowedTags = new Set(["P", "BR", "I", "EM", "B", "STRONG", "UL", "OL", "LI", "BLOCKQUOTE", "SUB", "SUP", "A"]);

    function copyChildren(source, destination) {
      for (const child of source.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
          destination.append(document.createTextNode(child.nodeValue || ""));
          continue;
        }
        if (child.nodeType !== Node.ELEMENT_NODE) continue;
        if (!allowedTags.has(child.tagName)) {
          copyChildren(child, destination);
          continue;
        }
        const safeNode = document.createElement(child.tagName.toLowerCase());
        if (child.tagName === "A") {
          const href = safeHttpUrl(child.getAttribute("href") || "");
          if (href) {
            safeNode.href = href;
            safeNode.target = "_blank";
            safeNode.rel = "noopener noreferrer";
          }
        }
        copyChildren(child, safeNode);
        destination.append(safeNode);
      }
    }

    copyChildren(parsed.body, target);
  }

  function showMessage(text, kind = "") {
    elements.message.textContent = text;
    elements.message.className = `message${kind ? ` ${kind}` : ""}`;
    elements.message.hidden = !text;
  }

  function showStartupMessage(text, kind = "") {
    elements.startupMessage.textContent = text;
    elements.startupMessage.className = `message${kind ? ` ${kind}` : ""}`;
    elements.startupMessage.hidden = !text;
  }

  function closeQuickMenu({ restoreFocus = false } = {}) {
    elements.quickNavToggle.setAttribute("aria-expanded", "false");
    elements.quickNavToggle.setAttribute("aria-label", "Open quick navigation");
    elements.quickMenuItems.hidden = true;
    elements.quickMenuBackdrop.hidden = true;
    document.body.classList.remove("quick-menu-open");
    if (restoreFocus) elements.quickNavToggle.focus();
  }

  function showHomePage({ restoreScroll = true } = {}) {
    state.currentPage = "home";
    elements.homeSticky.hidden = false;
    elements.discover.hidden = Boolean(state.query);
    elements.resultsSection.hidden = !state.query;
    elements.welcome.hidden = true;
    elements.recommendations.hidden = true;
    document.body.classList.remove("recommendations-active");
    document.body.classList.toggle("search-active", Boolean(state.query));
    elements.quickNavToggle.setAttribute("aria-label", "Open quick navigation");
    closeQuickMenu();
    if (restoreScroll) window.requestAnimationFrame(() => window.scrollTo(0, state.homeScrollY));
  }

  function showRecommendationsPage() {
    if (state.currentPage !== "recommendations") state.homeScrollY = window.scrollY;
    state.currentPage = "recommendations";
    closeQuickMenu();
    elements.homeSticky.hidden = true;
    elements.discover.hidden = true;
    elements.welcome.hidden = true;
    elements.resultsSection.hidden = true;
    elements.recommendations.hidden = false;
    document.body.classList.add("recommendations-active");
    document.body.classList.remove("search-active");
    elements.quickNavToggle.setAttribute("aria-label", "Return home");
    window.scrollTo(0, 0);
    elements.quickNavToggle.focus();
  }

  function toggleQuickMenu(forceOpen) {
    if (state.currentPage === "recommendations") {
      showHomePage();
      return;
    }
    const shouldOpen = forceOpen ?? elements.quickMenuItems.hidden;
    if (!shouldOpen) {
      closeQuickMenu({ restoreFocus: true });
      return;
    }
    elements.quickMenuItems.hidden = false;
    elements.quickMenuBackdrop.hidden = false;
    elements.quickNavToggle.setAttribute("aria-expanded", "true");
    elements.quickNavToggle.setAttribute("aria-label", "Close quick navigation");
    document.body.classList.add("quick-menu-open");
    elements.quickMenuItems.querySelector('[data-page="recommendations"]').focus();
  }

  async function api(path, options = {}) {
    const response = await fetch(`/miniapp/api/${path}`, {
      ...options,
      headers: {
        "X-Telegram-Init-Data": state.initData,
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers,
      },
      signal: options.signal,
    });
    let payload;
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) {
      const error = new Error(payload.error || `http_${response.status}`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function explainError(error) {
    if (error.status === 401) return "Telegram could not verify this session. Close and reopen the Mini App from the bot.";
    if (error.status === 429 || error.message === "rate_limited") return "You’re searching quickly. Please wait a moment and try again.";
    if (error.message === "service_unavailable") return "Search is starting up. Please try again in a moment.";
    if (error.message === "invalid_query") return "Enter at least 2 characters to search.";
    return "The search could not be completed. Check your connection and try again.";
  }

  function setBusy(busy) {
    elements.searchButton.disabled = busy || !state.ready;
    // Keep the input editable while a request is in flight so the user can
    // refine the query; the previous request is aborted/ignored on new input.
    elements.input.disabled = !state.ready;
    elements.previous.disabled = busy || state.page <= 1;
    elements.next.disabled = busy || !state.hasMore;
  }

  function appendCover(container, url, title, className = "cover") {
    const safeUrl = safeHttpUrl(url);
    if (!safeUrl) {
      container.append(node("div", "cover-placeholder", "▤"));
      return;
    }
    const image = node("img", className);
    image.src = safeUrl;
    image.alt = `Cover of ${title}`;
    image.loading = "lazy";
    image.decoding = "async";
    image.referrerPolicy = "no-referrer";
    image.addEventListener("error", () => {
      const placeholder = node("div", "cover-placeholder", "▤");
      image.replaceWith(placeholder);
    }, { once: true });
    container.append(image);
  }

  function renderBookCard(book, bookIndex, context) {
    const cardClass = context.collection === "featured" ? "trending-card"
      : context.collection === "more" ? "book-card more-card" : "book-card";
    const card = node("button", cardClass);
    card.type = "button";
    card.dataset.bookIndex = String(bookIndex);
    card.setAttribute("aria-label", `View details for ${book.title || "untitled book"}`);
    appendCover(card, book.cover_url, book.title || "book");

    const copy = node("div", "book-copy");
    copy.append(node("h3", "book-title", book.title || "Untitled"));
    copy.append(node("p", "book-author", book.author || "Unknown author"));
    copy.append(makeRatingDisplay(book, "book-rating"));

    const categories = Array.isArray(book.categories) ? book.categories.slice(0, 2) : [];
    if (categories.length) {
      const chips = node("div", "book-categories");
      for (const category of categories) {
        if (typeof category === "string" && category.trim()) chips.append(node("span", "category-chip", category));
      }
      copy.append(chips);
    }
    card.append(copy);
    card.addEventListener("click", () => openDetails(book, {
      ...context,
      index: context.collection === "featured" || context.collection === "more" ? bookIndex % 5 : bookIndex,
      absoluteIndex: bookIndex,
      page: context.collection === "featured" || context.collection === "more"
        ? Math.floor(bookIndex / 5) + 1 : context.page,
    }));
    return card;
  }

  function setDiscoverMessage(text, kind = "") {
    elements.discoverMessage.textContent = text;
    elements.discoverMessage.className = `message${kind ? ` ${kind}` : ""}`;
    elements.discoverMessage.hidden = !text;
  }

  function renderBookLoader(message, variant) {
    const loader = node("div", `book-loader ${variant}`, undefined);
    loader.setAttribute("role", "status");
    loader.setAttribute("aria-live", "polite");
    const icon = node("span", "book-loader-icon");
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = '<svg viewBox="0 0 32 32" focusable="false"><path d="M16 8.2c-3.2-2-7.1-2.1-10.3-.5v16.1c3.2-1.6 7.1-1.5 10.3.5m0-16.1c3.2-2 7.1-2.1 10.3-.5v16.1c-3.2-1.6-7.1-1.5-10.3.5m0-16.1v16.1"/><path class="book-loader-page" d="M8.5 12.2c2-.6 4-.3 5.7.6m-5.7 3c2-.6 4-.3 5.7.6m7-4.2c-1.1-.3-2.2-.3-3.3-.1m3.3 3.1c-1.1-.3-2.2-.3-3.3-.1"/></svg>';
    loader.append(icon, node("span", "book-loader-label", message));
    const dots = node("span", "book-loader-dots", "•••");
    dots.setAttribute("aria-hidden", "true");
    loader.append(dots);
    return loader;
  }

  function renderGenreTabs() {
    const genres = ["All", "Fantasy", "Romance", "Mystery", "Thriller", "Sci-Fi", "Horror", "Classics"];
    elements.genreTabs.replaceChildren();
    genres.forEach((genre) => {
      const tab = node("button", `genre-tab${state.featuredGenre === genre ? " active" : ""}`, genre);
      tab.type = "button";
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(state.featuredGenre === genre));
      tab.addEventListener("click", () => {
        if (state.featuredGenre === genre) return;
        state.featuredGenre = genre;
        renderGenreTabs();
        void loadTrending(genre);
      });
      elements.genreTabs.append(tab);
    });
  }

  async function loadTrending(genre) {
    const requestId = ++state.featuredRequestId;
    state.featuredBooks = [];
    elements.discoverTitle.textContent = genre === "All" ? "Top books" : `Top ${genre} books`;
    elements.trendingBooks.replaceChildren();
    elements.moreTrendingBooks.replaceChildren();
    elements.moreSection.hidden = true;
    const cached = state.featuredCache[genre];
    const cacheIsFresh = cached && Date.now() - cached.loadedAt < HOME_TRENDING_CACHE_MS;
    if (cached && !cacheIsFresh) {
      delete state.featuredCache[genre];
    }
    if (cacheIsFresh) {
      state.featuredBooks = cached.books;
      state.featuredQuery = cached.query;
      cached.books.slice(0, HOME_SHELF_SIZE).forEach((book, index) => {
        elements.trendingBooks.append(renderBookCard(book, index, {
          query: cached.query,
          page: 1,
          collection: "featured",
        }));
      });
      cached.books.slice(HOME_SHELF_SIZE).forEach((book, index) => {
        elements.moreTrendingBooks.append(renderBookCard(book, index + HOME_SHELF_SIZE, {
          query: cached.query,
          page: 2,
          collection: "more",
        }));
      });
      elements.moreSection.hidden = cached.books.length <= HOME_SHELF_SIZE;
      setDiscoverMessage(cached.books.length ? "" : "No picks found for this genre yet.");
      refreshDesktopScrollControls(elements.trendingBooks);
      return;
    }
    setDiscoverMessage("");
    elements.trendingBooks.append(renderBookLoader("Gathering books to explore", "home-loader"));
    try {
      const response = await api("trending", {
        method: "POST",
        body: JSON.stringify({ genre }),
      });
      if (requestId !== state.featuredRequestId || elements.input.value.trim()) return;
      state.featuredQuery = response.data.query;
      state.featuredBooks = response.data.books;
      elements.trendingBooks.replaceChildren();
      if (!state.featuredBooks.length) {
        setDiscoverMessage("No picks found for this genre yet.");
        return;
      }
      state.featuredCache[genre] = {
        query: state.featuredQuery,
        books: state.featuredBooks,
        loadedAt: Date.now(),
      };
      setDiscoverMessage("");
      state.featuredBooks.slice(0, HOME_SHELF_SIZE).forEach((book, index) => {
        elements.trendingBooks.append(renderBookCard(book, index, {
          query: state.featuredQuery,
          page: 1,
          collection: "featured",
        }));
      });
      state.featuredBooks.slice(HOME_SHELF_SIZE).forEach((book, index) => {
        elements.moreTrendingBooks.append(renderBookCard(book, index + HOME_SHELF_SIZE, {
          query: state.featuredQuery,
          page: 2,
          collection: "more",
        }));
      });
      elements.moreSection.hidden = state.featuredBooks.length <= HOME_SHELF_SIZE;
      refreshDesktopScrollControls(elements.trendingBooks);
    } catch (error) {
      if (requestId !== state.featuredRequestId || error.name === "AbortError") return;
      elements.trendingBooks.replaceChildren();
      setDiscoverMessage(explainError(error), "error");
    }
  }

  function makeRatingDisplay(book, className) {
    const rating = node("p", className);
    if (Number(book.rating) > 0) {
      rating.append(
        node("span", "star", "★ "),
        document.createTextNode(`${Number(book.rating).toFixed(2)}${Number(book.rating_count) > 0 ? ` · ${Number(book.rating_count).toLocaleString()} ratings` : ""}`),
      );
    } else {
      rating.textContent = "No rating available";
    }
    return rating;
  }

  async function copyToClipboard(value) {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
        return true;
      }
    } catch (_) { /* Fall through for embedded webviews without clipboard permission. */ }

    const temporary = document.createElement("textarea");
    temporary.value = value;
    temporary.setAttribute("readonly", "");
    temporary.style.position = "fixed";
    temporary.style.opacity = "0";
    document.body.append(temporary);
    temporary.select();
    let copied = false;
    try { copied = document.execCommand("copy"); } catch (_) { copied = false; }
    temporary.remove();
    return copied;
  }

  function createCopyControl(label, value) {
    const button = node("button", "copy-button");
    button.type = "button";
    button.setAttribute("aria-label", `Copy ${label}`);
    button.title = `Copy ${label}`;
    const status = node("span", "metadata-copy-status");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    let resetTimer = null;
    const setIcon = (copied) => {
      button.innerHTML = copied
        ? '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m4 10 4 4 8-8"/></svg>'
        : '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="7" y="6" width="9" height="11" rx="1.5"/><path d="M13 6V4.5A1.5 1.5 0 0 0 11.5 3h-7A1.5 1.5 0 0 0 3 4.5v9A1.5 1.5 0 0 0 4.5 15H7"/></svg>';
    };
    setIcon(false);
    button.addEventListener("click", async () => {
      window.clearTimeout(resetTimer);
      button.disabled = true;
      const copied = await copyToClipboard(String(value));
      button.disabled = false;
      status.textContent = copied ? "Copied successfully" : "Couldn’t copy";
      status.classList.toggle("copied", copied);
      button.classList.toggle("copied", copied);
      button.title = copied ? "Copied successfully" : "Copy failed";
      button.setAttribute("aria-label", copied ? `${label} copied successfully` : `Could not copy ${label}`);
      setIcon(copied);
      if (copied) resetTimer = window.setTimeout(() => {
        if (!button.isConnected) return;
        status.textContent = "";
        status.classList.remove("copied");
        button.classList.remove("copied");
        button.title = `Copy ${label}`;
        button.setAttribute("aria-label", `Copy ${label}`);
        setIcon(false);
      }, 1800);
    });
    return { button, status };
  }

  function formatMetadataSource(value) {
    const source = String(value || "").replaceAll("_", " ").trim();
    return source
      .replace(/\bhardcover\b/gi, "Hardcover")
      .replace(/\bgoogle\s*books?\b/gi, "Google Books")
      .replace(/^./u, (first) => first.toLocaleUpperCase());
  }

  function shareBook(book) {
    const title = String(book.title || "").trim();
    if (!title) return;
    const author = String(book.author || "").trim();
    const query = `${title}${author && !/^unknown(?: author)?$/i.test(author) ? ` by ${author}` : ""}`.slice(0, 256);
    if (typeof webApp?.switchInlineQuery !== "function") {
      const message = "Sharing from a book page requires a Telegram client that supports inline sharing.";
      if (typeof webApp?.showAlert === "function") webApp.showAlert(message);
      else window.alert(message);
      return;
    }
    webApp.HapticFeedback?.impactOccurred("light");
    try {
      // Return to the chosen chat with Annie's inline query in the composer.
      // The user selects the exact inline result before sending it.
      webApp.switchInlineQuery(query, ["users", "groups"]);
    } catch (_) {
      const message = "Telegram couldn’t open the chat picker. Update Telegram and try again.";
      if (typeof webApp.showAlert === "function") webApp.showAlert(message);
      else window.alert(message);
    }
  }

  function addMetadata(container, label, value, { collapsible = false } = {}) {
    if (value === undefined || value === null || String(value).trim() === "") return;
    const item = node("section", `metadata-item${collapsible ? " metadata-genres" : ""}`);
    const header = node("div", "metadata-head");
    header.append(node("span", "metadata-label", label));
    const actions = node("div", "metadata-actions");

    if (collapsible) {
      const toggle = node("button", "genre-toggle", "Show all");
      toggle.type = "button";
      toggle.setAttribute("aria-expanded", "false");
      toggle.addEventListener("click", () => {
        const expanded = item.classList.toggle("expanded");
        toggle.setAttribute("aria-expanded", String(expanded));
        toggle.textContent = expanded ? "Show less" : "Show all";
      });
      actions.append(toggle);
    }

    const displayValue = label === "Language" ? formatLanguage(value) : String(value);
    const copyControl = createCopyControl(label, displayValue);
    actions.append(copyControl.button);
    header.append(actions);

    const metadataValue = node("span", "metadata-value", displayValue);
    item.append(header, metadataValue, copyControl.status);
    container.append(item);
  }

  function formatLanguage(value) {
    const language = String(value || "").trim();
    if (!language) return "";
    if (!/^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$/i.test(language)) return language;
    try {
      return new Intl.DisplayNames(["en"], { type: "language" }).of(language) || language;
    } catch (_) {
      return language;
    }
  }

  function renderBookDetails(book, loadingMessage = "", context = state.activeDetailContext) {
    elements.detail.replaceChildren();
    const hero = node("section", "detail-hero");
    hero.setAttribute("aria-label", "Selected book");
    if (book.cover_url) appendCover(hero, book.cover_url, book.title || "book", "detail-cover");
    else hero.append(node("div", "detail-placeholder", "▤"));

    const titleRow = node("div", "detail-title-row");
    titleRow.append(node("h2", "detail-title", book.title || "Untitled"));
    const shareButton = node("button", "share-book-button");
    shareButton.type = "button";
    shareButton.setAttribute("aria-label", "Share this book in a Telegram chat");
    shareButton.title = "Share this book";
    shareButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.7 10.6 6.6-4.2m-6.6 7 6.6 4.2"/></svg>';
    shareButton.addEventListener("click", () => shareBook(book));
    titleRow.append(shareButton);
    hero.append(titleRow);
    if (book.show_translation && book.translated_title
      && String(book.translated_title).toLocaleLowerCase() !== String(book.title || "").toLocaleLowerCase()) {
      hero.append(node("p", "detail-translated-title", `English title: ${book.translated_title}`));
    }
    hero.append(node("p", "detail-author", book.author || "Unknown author"));
    hero.append(makeRatingDisplay(book, "detail-rating"));
    if (loadingMessage) hero.append(node("p", "detail-status", loadingMessage));
    elements.detail.append(hero);

    const metadata = node("div", "metadata");
    addMetadata(metadata, "Genres", Array.isArray(book.categories) ? book.categories.join(", ") : "", { collapsible: true });
    addMetadata(metadata, "Published", book.published_date);
    addMetadata(metadata, "Pages", book.page_count);
    addMetadata(metadata, "Language", book.language);
    addMetadata(metadata, "ISBN", book.isbn);
    addMetadata(metadata, "Source", formatMetadataSource(book.metadata_source || book.source));
    if (metadata.childElementCount) elements.detail.append(metadata);

    const needsTranslation = book.description_needs_translation || book.title_needs_translation;
    if (book.description) {
      const descriptionSection = node("section", "description-section");
      const descriptionHeading = node("div", "description-heading");
      descriptionHeading.append(node("h3", "detail-section-title", "Description"));
      const descriptionActions = node("div", "description-actions");
      let translationStatus = null;
      if (needsTranslation) {
        const button = node("button", "translate-button", book.show_translation ? "Show original" : "Translate");
        button.type = "button";
        translationStatus = node("span", "translation-status");
        button.addEventListener("click", () => {
          void toggleBookTranslation(book, context, button, translationStatus);
        });
        descriptionActions.append(button);
      }
      const description = book.show_translation && book.translated_description
        ? book.translated_description : book.description;
      const descriptionContent = node("div", "detail-description");
      appendSafeDescription(descriptionContent, description);
      const descriptionCopy = createCopyControl(
        "Description",
        descriptionContent.innerText || descriptionContent.textContent || description,
      );
      descriptionActions.append(descriptionCopy.button);
      descriptionHeading.append(descriptionActions);
      descriptionSection.append(descriptionHeading);
      descriptionSection.append(descriptionContent);
      descriptionCopy.status.classList.add("description-copy-status");
      descriptionSection.append(descriptionCopy.status);
      if (translationStatus) descriptionSection.append(translationStatus);
      elements.detail.append(descriptionSection);
    } else if (needsTranslation) {
      const translationActions = node("div", "description-actions title-translation-actions");
      const button = node("button", "translate-button", book.show_translation ? "Show original" : "Translate");
      const translationStatus = node("span", "translation-status");
      button.type = "button";
      button.addEventListener("click", () => {
        void toggleBookTranslation(book, context, button, translationStatus);
      });
      translationActions.append(button, translationStatus);
      elements.detail.append(translationActions);
    }
    const relatedBooks = state.activeDetailContext === context && state.activeRelatedBooks.length
      ? state.activeRelatedBooks
      : (Array.isArray(book.related_books) ? book.related_books : []);
    if (context) {
      const relatedSection = node("section", "related-section");
      relatedSection.append(node("h3", "detail-section-title", "More Like This"));
      if (relatedBooks.length) {
        const shelf = node("div", "related-shelf");
        for (const relatedBook of relatedBooks) {
          if (!relatedBook) continue;
          const relatedCard = node("button", "related-card");
          relatedCard.type = "button";
          relatedCard.setAttribute("aria-label", `View details for ${relatedBook.title || "book"}`);
          appendCover(relatedCard, relatedBook.cover_url, relatedBook.title || "book", "related-cover");
          relatedCard.append(node("span", "related-title", relatedBook.title || "Untitled"));
          relatedCard.append(node("span", "related-author", relatedBook.author || "Unknown author"));
          relatedCard.addEventListener("click", () => openDetails(relatedBook, {
            collection: "related",
            book: { ...relatedBook },
          }));
          shelf.append(relatedCard);
        }
        relatedSection.append(addDesktopScrollControls(shelf, "More Like This"));
      } else {
        relatedSection.append(state.relatedLoading
          ? renderBookLoader("Finding books like this", "related-loader")
          : node("p", "related-status", state.relatedMessage || "No similar books found yet."));
      }
      elements.detail.append(relatedSection);
    }
  }

  async function toggleBookTranslation(book, context, button, status) {
    if (book.show_translation) {
      book.show_translation = false;
      renderBookDetails(book, "", context);
      return;
    }
    if (book.translated_title || book.translated_description) {
      book.show_translation = true;
      renderBookDetails(book, "", context);
      return;
    }
    if (!context) return;

    const requestId = ++state.translationRequestId;
    const controller = new AbortController();
    state.translationController?.abort();
    state.translationController = controller;
    button.disabled = true;
    button.textContent = "Translating…";
    status.textContent = "";
    try {
      const response = await api("translate", {
        method: "POST",
        body: JSON.stringify(["recommendations", "related"].includes(context.collection)
          ? { book: { title: book.title, author: book.author, description: book.description } }
          : { query: context.query, page: context.page, index: context.index }),
        signal: controller.signal,
      });
      if (requestId !== state.translationRequestId || state.activeDetailContext !== context || !elements.dialog.open) return;
      if (!response.data.any_translated && !response.data.translated && !response.data.title_translated) {
        status.textContent = "Translation isn’t available right now.";
        button.disabled = false;
        button.textContent = "Translate";
        return;
      }
      book.translated_title = response.data.title_translation || book.title;
      book.translated_description = response.data.translation || book.description;
      book.show_translation = true;
      renderBookDetails(book, "", context);
    } catch (error) {
      if (error.name !== "AbortError" && requestId === state.translationRequestId) {
        status.textContent = explainError(error);
        button.disabled = false;
        button.textContent = "Translate";
      }
    } finally {
      if (state.translationController === controller) state.translationController = null;
    }
  }

  function openDetails(book, context = null) {
    state.relatedRequestId += 1;
    state.relatedController?.abort();
    state.relatedController = null;
    state.currentDetailBook = book;
    state.activeRelatedBooks = [];
    state.relatedLoading = Boolean(context);
    state.relatedMessage = "";
    state.detailLoadingMessage = ["recommendations", "related"].includes(context?.collection)
      ? "Loading complete book details…" : "";
    state.translationRequestId += 1;
    state.translationController?.abort();
    state.translationController = null;
    state.activeDetailContext = context;
    elements.detailSearchInput.value = "";
    renderBookDetails(
      book,
      state.detailLoadingMessage,
      context,
    );
    if (typeof elements.dialog.showModal === "function") elements.dialog.showModal();
    else elements.dialog.setAttribute("open", "");
    elements.quickNav.hidden = true;
    webApp?.HapticFeedback?.impactOccurred("light");
    if (["recommendations", "related"].includes(context?.collection)) {
      void fetchRecommendationBookDetails(context);
    } else if (context) {
      void fetchGoogleBookDetails(context);
    }
  }

  function renderResults(data) {
    state.page = data.page;
    state.pageSize = data.page_size;
    state.total = data.total;
    state.hasMore = data.has_more;
    elements.welcome.hidden = true;
    elements.resultsSection.hidden = false;
    elements.resultsTitle.textContent = data.query;
    elements.resultCount.textContent = `${data.total} ${data.total === 1 ? "book" : "books"}`;
    elements.results.replaceChildren();
    state.visibleBooks = data.books.slice();

    if (!data.books.length) {
      showMessage("No books found for this search. Try a different title or author.");
    } else {
      showMessage("");
    state.visibleBooks.forEach((book, index) => elements.results.append(renderBookCard(book, index, {
      query: state.query,
      page: state.page,
      collection: "results",
    })));
    }

    const pageCount = Math.max(1, Math.ceil(data.total / data.page_size));
    elements.pageLabel.textContent = `Page ${data.page} of ${pageCount}`;
    elements.previous.disabled = data.page <= 1;
    elements.next.disabled = !data.has_more;
    elements.pagination.hidden = data.total <= data.page_size && data.page <= 1;
  }

  async function search(query, page = 1) {
    if (!state.ready) return;
    state.query = query.trim();
    document.body.classList.toggle("search-active", Boolean(state.query));
    state.page = page;
    const requestId = ++state.requestId;
    state.activeController?.abort();
    state.detailsRequestId += 1;
    state.detailsController?.abort();
    state.detailsController = null;
    const controller = new AbortController();
    state.activeController = controller;
    elements.welcome.hidden = true;
    elements.resultsSection.hidden = false;
    elements.resultsTitle.textContent = state.query;
    elements.results.replaceChildren();
    elements.pagination.hidden = true;
    showMessage("Searching book catalogs…", "loading");
    elements.typingIndicator.hidden = true;
    setBusy(true);
    try {
      const response = await api("search", {
        method: "POST",
        body: JSON.stringify({ query: state.query, page }),
        signal: controller.signal,
      });
      if (requestId !== state.requestId) return;
      renderResults(response.data);
      webApp?.HapticFeedback?.notificationOccurred("success");
    } catch (error) {
      if (error.name === "AbortError" || requestId !== state.requestId) return;
      showMessage(explainError(error), "error");
      elements.results.replaceChildren();
      elements.pagination.hidden = true;
      webApp?.HapticFeedback?.notificationOccurred("error");
    } finally {
      if (requestId === state.requestId) {
        state.activeController = null;
        setBusy(false);
      }
    }
  }

  async function fetchGoogleBookDetails(context) {
    const requestId = ++state.detailsRequestId;
    const { query, page, index } = context;
    const controller = new AbortController();
    state.detailsController?.abort();
    state.detailsController = controller;
    try {
      const response = await api("details", {
        method: "POST",
        body: JSON.stringify({ query, page, index }),
        signal: controller.signal,
      });
      if (requestId !== state.detailsRequestId || state.activeDetailContext !== context || !elements.dialog.open) return;
      if (context.collection === "featured" || context.collection === "more") {
        state.featuredBooks[context.absoluteIndex] = response.data.book;
      } else if (context.collection !== "related") state.visibleBooks[index] = response.data.book;
      const localRelated = Array.isArray(response.data.related_books)
        ? response.data.related_books.map((item) => item && item.book).filter(Boolean) : [];
      if (!state.activeRelatedBooks.length) state.activeRelatedBooks = localRelated;
      response.data.book.related_books = state.activeRelatedBooks;
      state.currentDetailBook = response.data.book;
      state.detailLoadingMessage = "";
      renderBookDetails(response.data.book, state.detailLoadingMessage, context);
      void fetchRelatedBooks(context, response.data.book);
    } catch (error) {
      if (error.name !== "AbortError" && requestId === state.detailsRequestId) {
        state.detailLoadingMessage = "";
        renderBookDetails(state.currentDetailBook, "", context);
        void fetchRelatedBooks(context, state.currentDetailBook);
      }
    } finally {
      if (state.detailsController === controller) state.detailsController = null;
    }
  }

  async function fetchRecommendationBookDetails(context) {
    const requestId = ++state.detailsRequestId;
    const controller = new AbortController();
    state.detailsController?.abort();
    state.detailsController = controller;
    try {
      const response = await api("recommendation-details", {
        method: "POST",
        body: JSON.stringify({ book: context.book }),
        signal: controller.signal,
      });
      if (requestId !== state.detailsRequestId || state.activeDetailContext !== context || !elements.dialog.open) return;
      const enriched = response.data.book || context.book;
      for (const field of ["cover_url", "rating", "rating_count", "description", "categories", "isbn", "page_count", "published_date", "language", "info_link"]) {
        const value = enriched[field];
        if ((value === undefined || value === null || value === "" || (Array.isArray(value) && !value.length)) && context.book[field] !== undefined) {
          enriched[field] = context.book[field];
        }
      }
      state.currentDetailBook = enriched;
      state.detailLoadingMessage = "";
      renderBookDetails(enriched, "", context);
      void fetchRelatedBooks(context, enriched);
    } catch (error) {
      if (error.name !== "AbortError" && requestId === state.detailsRequestId && state.activeDetailContext === context) {
        state.detailLoadingMessage = "";
        renderBookDetails(state.currentDetailBook, "", context);
        void fetchRelatedBooks(context, state.currentDetailBook);
      }
    } finally {
      if (state.detailsController === controller) state.detailsController = null;
    }
  }

  async function fetchRelatedBooks(context, selectedBook = state.currentDetailBook) {
    const requestId = ++state.relatedRequestId;
    const controller = new AbortController();
    state.relatedController?.abort();
    state.relatedController = controller;
    if (!selectedBook) return;
    try {
      const response = await api("related", {
        method: "POST",
        body: JSON.stringify({
          book: {
            title: selectedBook.title,
            author: selectedBook.author,
            categories: selectedBook.categories,
            isbn: selectedBook.isbn,
          },
        }),
        signal: controller.signal,
      });
      if (requestId !== state.relatedRequestId || state.activeDetailContext !== context || !elements.dialog.open) return;
      state.activeRelatedBooks = Array.isArray(response.data.books) ? response.data.books : [];
      state.relatedLoading = false;
      state.relatedMessage = "";
      if (state.currentDetailBook) {
        state.currentDetailBook.related_books = state.activeRelatedBooks;
        renderBookDetails(state.currentDetailBook, state.detailLoadingMessage, context);
      }
    } catch (error) {
      if (error.name !== "AbortError" && requestId === state.relatedRequestId) {
        state.relatedLoading = false;
        state.relatedMessage = "Couldn’t load suggestions right now.";
        if (state.currentDetailBook && state.activeDetailContext === context) {
          renderBookDetails(state.currentDetailBook, state.detailLoadingMessage, context);
        }
      }
    } finally {
      if (state.relatedController === controller) state.relatedController = null;
    }
  }

  function resetResultsForInput(query) {
    state.requestId += 1;
    state.activeController?.abort();
    state.detailsRequestId += 1;
    state.detailsController?.abort();
    state.activeController = null;
    state.detailsController = null;
    state.query = query;
    state.page = 1;
    state.total = 0;
    state.hasMore = false;
    state.visibleBooks = [];
    document.body.classList.toggle("search-active", Boolean(query));
    elements.results.replaceChildren();
    elements.pagination.hidden = true;
    elements.moreSection.hidden = true;
    if (!query) {
      elements.resultsSection.hidden = true;
      elements.discover.hidden = false;
      elements.welcome.hidden = true;
      showMessage("");
      elements.typingIndicator.hidden = true;
      setBusy(false);
      void loadTrending(state.featuredGenre);
      return;
    }
    elements.discover.hidden = true;
    elements.welcome.hidden = true;
    elements.resultsSection.hidden = false;
    elements.resultsTitle.textContent = query;
    elements.resultCount.textContent = "";
    elements.typingIndicator.hidden = query.length < 2;
    showMessage(query.length < 3 ? "Type at least 3 characters to search." : "Searching when you pause typing…", "loading");
    setBusy(false);
  }

  async function initialize() {
    setBusy(true);
    if (webApp) {
      webApp.ready();
      webApp.expand();
      try { webApp.setHeaderColor?.(THEME_COLORS[document.documentElement.dataset.theme] || THEME_COLORS.purple); } catch (_) { /* Older Telegram clients */ }
      try { webApp.setBackgroundColor?.(THEME_COLORS[document.documentElement.dataset.theme] || THEME_COLORS.purple); } catch (_) { /* Older Telegram clients */ }
    }

    state.initData = webApp?.initData || "";
    if (!state.initData) {
      showStartupMessage("Open this page inside Telegram from your bot to search books.", "error");
      dismissLaunchWelcome();
      return;
    }

    try {
      const response = await api("session");
      state.ready = true;
      showStartupMessage("");
      elements.greeting.textContent = response.user.first_name
        ? `Hi, ${response.user.first_name}`
        : "Hi there";
      setBusy(false);
      renderGenreTabs();
      if (new URLSearchParams(window.location.search).get("page") === "recommendations") {
        showRecommendationsPage();
        dismissLaunchWelcome();
        return;
      }
      await loadTrending("All");
    } catch (error) {
      showStartupMessage(explainError(error), "error");
    } finally {
      dismissLaunchWelcome();
    }
  }

  elements.form.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = elements.input.value.trim();
    window.clearTimeout(state.debounceTimer);
    if (query.length >= 2) search(query, 1);
    else elements.input.focus();
  });
  elements.quickNavToggle.addEventListener("click", () => toggleQuickMenu());
  elements.quickMenuBackdrop.addEventListener("click", () => closeQuickMenu({ restoreFocus: true }));
  elements.quickMenuItems.querySelector('[data-page="recommendations"]').addEventListener("click", showRecommendationsPage);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !elements.quickMenuItems.hidden) {
      event.preventDefault();
      closeQuickMenu({ restoreFocus: true });
    }
  });
  elements.input.addEventListener("input", () => {
    const query = elements.input.value.trim();
    window.clearTimeout(state.debounceTimer);
    resetResultsForInput(query);
    if (query.length >= 3) {
      state.debounceTimer = window.setTimeout(() => search(query, 1), 650);
    }
  });
  elements.previous.addEventListener("click", () => {
    if (state.page > 1) search(state.query, state.page - 1);
  });
  elements.next.addEventListener("click", () => {
    if (state.hasMore) search(state.query, state.page + 1);
  });
  elements.closeDialog.addEventListener("click", () => elements.dialog.close());
  elements.homeButton.addEventListener("click", () => {
    elements.dialog.close();
    elements.input.value = "";
    elements.detailSearchInput.value = "";
    resetResultsForInput("");
    window.scrollTo({ top: 0, behavior: "auto" });
  });
  elements.detailSearchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = elements.detailSearchInput.value.trim();
    window.clearTimeout(state.debounceTimer);
    if (query.length < 2) {
      elements.detailSearchInput.focus();
      return;
    }
    elements.input.value = query;
    elements.dialog.close();
    void search(query, 1);
  });
  elements.detailSearchInput.addEventListener("input", () => {
    const query = elements.detailSearchInput.value.trim();
    elements.input.value = query;
    window.clearTimeout(state.debounceTimer);
    resetResultsForInput(query);
    if (query.length >= 3) {
      state.debounceTimer = window.setTimeout(() => {
        elements.dialog.close();
        void search(query, 1);
      }, 650);
    }
  });
  elements.dialog.addEventListener("close", () => {
    elements.quickNav.hidden = false;
    state.detailsRequestId += 1;
    state.detailsController?.abort();
    state.detailsController = null;
    state.translationRequestId += 1;
    state.translationController?.abort();
    state.translationController = null;
    state.relatedRequestId += 1;
    state.relatedController?.abort();
    state.relatedController = null;
    state.relatedLoading = false;
    state.relatedMessage = "";
    state.currentDetailBook = null;
    state.activeRelatedBooks = [];
    state.activeDetailContext = null;
  });
  elements.dialog.addEventListener("click", (event) => {
    if (event.target === elements.dialog) elements.dialog.close();
  });

  addDesktopScrollControls(elements.trendingBooks, "Top Books");
  window.addEventListener("resize", () => {
    document.querySelectorAll(".trending-books, .related-shelf").forEach(refreshDesktopScrollControls);
    updateShelfInstruction();
  }, { passive: true });
  updateShelfInstruction();
  initialize();
})();
