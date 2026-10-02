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
    featuredBookSlot: document.querySelector("#featured-book-slot"),
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
    bookshelf: document.querySelector("#bookshelf-view"),
    bookshelfConfirm: document.querySelector("#bookshelf-confirm-dialog"),
    quickNav: document.querySelector("#quick-nav"),
    quickNavToggle: document.querySelector("#quick-nav-toggle"),
    quickMenuItems: document.querySelector("#quick-menu-items"),
    quickMenuBackdrop: document.querySelector("#quick-menu-backdrop"),
    appTabbar: document.querySelector("#app-tabbar"),
    themeButtons: Array.from(document.querySelectorAll("[data-theme-choice]")),
  };

  const state = {
    initData: "", inlineSessionToken: "", query: "", page: 1, pageSize: 5, total: 0,
    hasMore: false, ready: false, requestId: 0, activeController: null,
    detailsController: null, detailsRequestId: 0, translationController: null,
    translationRequestId: 0, relatedController: null, relatedRequestId: 0,
    currentDetailBook: null, activeRelatedBooks: [], relatedLoading: false,
    relatedMessage: "", detailLoadingMessage: "", visibleBooks: [], selectedIndex: null,
    debounceTimer: null, activeDetailContext: null, featuredQuery: "",
    featuredBooks: [], featuredGenre: "All", featuredRequestId: 0, featuredCache: {},
    currentPage: "home", previousPage: "home", searchFocused: false, homeScrollY: 0, bookshelfAddMode: false,
    bookshelf: { saved: [], favorites: [] }, bookshelfUi: {
      saved: { query: "", sort: "newest", view: "grid", selecting: false, selected: [] },
      favorites: { query: "", sort: "newest", view: "grid", selecting: false, selected: [] },
    },
    bookshelfCloudEnabled: false, bookshelfLoaded: false, bookshelfLoading: false,
    bookshelfLastLoadedAt: 0, bookshelfError: "", bookshelfLimit: 100, bookshelfRevision: 0,
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
  let BOOKSHELF_STORAGE_KEY = `annie-bookshelf-v1:${webApp?.initDataUnsafe?.user?.id || "guest"}`;
  const HOME_SHELF_SIZE = 10;
  const HOME_TRENDING_CACHE_MS = 60 * 60 * 1000;
  const HOME_TRENDING_CACHE_VERSION = "v4";
  const THEME_COLORS = { purple: "#111c1b", light: "#e4eee8", amoled: "#000000" };

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
  }, renderBookLoader);

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
    elements.message.replaceChildren();
    if (kind === "loading" && text) {
      elements.message.append(renderBookLoader(text, "message-loader"));
    } else {
      elements.message.textContent = text;
    }
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

  function updateAppTabbar() {
    if (!elements.appTabbar) return;
    const activeTab = state.currentPage === "home"
      ? (state.searchFocused || state.query ? "search" : "discover")
      : state.currentPage;
    elements.appTabbar.querySelectorAll("[data-app-tab]").forEach((button) => {
      const active = button.dataset.appTab === activeTab;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  }

  function navigateBack() {
    const destination = state.previousPage || "home";
    if (destination === "bookshelf") showBookshelfPage();
    else if (destination === "recommendations") showRecommendationsPage();
    else showHomePage({ restoreScroll: true, searchFocused: state.searchFocused });
  }

  function makePageBackButton(label) {
    const button = node("button", "page-back-button");
    button.type = "button";
    button.setAttribute("aria-label", `Back to ${label}`);
    button.title = `Back to ${label}`;
    button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14.5 5-7 7 7 7M8 12h12"/></svg>';
    button.addEventListener("click", navigateBack);
    return button;
  }

  function navigateToSearch() {
    if (state.currentPage !== "home") showHomePage({ restoreScroll: false, searchFocused: true });
    else {
      state.searchFocused = true;
      document.body.classList.add("search-focused");
      if (!state.query) {
        elements.discover.hidden = true;
        elements.resultsSection.hidden = true;
        elements.welcome.hidden = false;
      }
      updateAppTabbar();
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
    window.setTimeout(() => elements.input.focus({ preventScroll: true }), 80);
  }

  function showHomePage({ restoreScroll = true, searchFocused = false } = {}) {
    if (state.currentPage !== "home") state.previousPage = state.currentPage;
    state.currentPage = "home";
    state.searchFocused = searchFocused;
    elements.homeSticky.hidden = false;
    elements.discover.hidden = Boolean(state.query) || searchFocused;
    elements.resultsSection.hidden = !state.query;
    elements.welcome.hidden = !searchFocused || Boolean(state.query);
    elements.recommendations.hidden = true;
    elements.bookshelf.hidden = true;
    document.body.classList.remove("recommendations-active", "bookshelf-active");
    document.body.classList.toggle("search-active", Boolean(state.query));
    document.body.classList.toggle("search-focused", searchFocused);
    elements.quickNavToggle.setAttribute("aria-label", "Open quick navigation");
    closeQuickMenu();
    updateAppTabbar();
    if (restoreScroll) window.requestAnimationFrame(() => window.scrollTo(0, state.homeScrollY));
  }

  function showRecommendationsPage() {
    if (state.currentPage === "home") state.homeScrollY = window.scrollY;
    if (state.currentPage !== "recommendations") state.previousPage = state.currentPage;
    state.currentPage = "recommendations";
    closeQuickMenu();
    elements.homeSticky.hidden = true;
    elements.discover.hidden = true;
    elements.welcome.hidden = true;
    elements.resultsSection.hidden = true;
    elements.recommendations.hidden = false;
    let recommendationsBack = elements.recommendations.querySelector(".recommendations-back-row");
    if (!recommendationsBack) {
      recommendationsBack = node("div", "recommendations-back-row");
      recommendationsBack.append(makePageBackButton("previous page"));
    }
    elements.recommendations.prepend(recommendationsBack);
    elements.bookshelf.hidden = true;
    document.body.classList.remove("bookshelf-active");
    document.body.classList.remove("search-focused");
    document.body.classList.add("recommendations-active");
    document.body.classList.remove("search-active");
    elements.quickNavToggle.setAttribute("aria-label", "Return home");
    updateAppTabbar();
    window.scrollTo(0, 0);
  }

  function showBookshelfPage() {
    if (state.currentPage === "home") state.homeScrollY = window.scrollY;
    if (state.currentPage !== "bookshelf") state.previousPage = state.currentPage;
    state.currentPage = "bookshelf";
    closeQuickMenu();
    elements.homeSticky.hidden = true;
    elements.discover.hidden = true;
    elements.welcome.hidden = true;
    elements.resultsSection.hidden = true;
    elements.recommendations.hidden = true;
    elements.bookshelf.hidden = false;
    document.body.classList.remove("recommendations-active", "search-active");
    document.body.classList.remove("search-focused");
    document.body.classList.add("bookshelf-active");
    elements.quickNavToggle.setAttribute("aria-label", "Return home");
    updateAppTabbar();
    renderBookshelf();
    void syncRemoteBookshelf();
    window.scrollTo(0, 0);
  }

  function loadBookshelfCache() {
    try {
      const cached = JSON.parse(window.localStorage.getItem(BOOKSHELF_STORAGE_KEY) || "{}");
      state.bookshelf.saved = Array.isArray(cached.saved) ? cached.saved : [];
      state.bookshelf.favorites = Array.isArray(cached.favorites) ? cached.favorites : [];
      const favoriteIds = new Set(state.bookshelf.favorites.map((book) => book.id));
      const uniqueSaved = state.bookshelf.saved.filter((book) => !favoriteIds.has(book.id));
      if (uniqueSaved.length !== state.bookshelf.saved.length) {
        state.bookshelf.saved = uniqueSaved;
        persistBookshelfCache();
      }
    } catch (_) {
      state.bookshelf = { saved: [], favorites: [] };
    }
  }

  function setBookshelfCacheUser(userId) {
    const nextKey = `annie-bookshelf-v1:${userId || "guest"}`;
    if (nextKey === BOOKSHELF_STORAGE_KEY) return;
    try {
      const existing = window.localStorage.getItem(nextKey);
      if (existing) {
        const parsed = JSON.parse(existing);
        state.bookshelf = {
          saved: Array.isArray(parsed.saved) ? parsed.saved : [],
          favorites: Array.isArray(parsed.favorites) ? parsed.favorites : [],
        };
      } else if (BOOKSHELF_STORAGE_KEY.endsWith(":guest")) {
        window.localStorage.setItem(nextKey, JSON.stringify(state.bookshelf));
      }
    } catch (_) { /* Cloud storage remains available if local cache migration fails. */ }
    BOOKSHELF_STORAGE_KEY = nextKey;
  }

  function persistBookshelfCache() {
    try { window.localStorage.setItem(BOOKSHELF_STORAGE_KEY, JSON.stringify(state.bookshelf)); }
    catch (_) { /* Local cache is optional; the bookshelf remains usable for this session. */ }
  }

  async function syncRemoteBookshelf({ force = false } = {}) {
    if (!state.bookshelfCloudEnabled || state.bookshelfLoading) return;
    if (!force && state.bookshelfLoaded && Date.now() - state.bookshelfLastLoadedAt < 30_000) return;
    state.bookshelfLoading = true;
    state.bookshelfError = "";
    const revision = state.bookshelfRevision;
    if (!state.bookshelfLoaded && state.currentPage === "bookshelf") renderBookshelf();
    try {
      const response = await api("bookshelf");
      let remoteBookshelf = response.data?.bookshelf || { saved: [], favorites: [] };
      const hasRemoteBooks = (remoteBookshelf.saved?.length || 0) + (remoteBookshelf.favorites?.length || 0) > 0;
      const localEntries = [
        ...state.bookshelf.saved.map((book) => ({ collection: "saved", book, addedAt: book.addedAt })),
        ...state.bookshelf.favorites.map((book) => ({ collection: "favorites", book, addedAt: book.addedAt })),
      ];
      if (!hasRemoteBooks && localEntries.length) {
        const imported = await api("bookshelf", {
          method: "POST",
          body: JSON.stringify({ action: "import_if_empty", entries: localEntries }),
        });
        if (!imported.data?.imported) {
          remoteBookshelf = (await api("bookshelf")).data?.bookshelf || { saved: [], favorites: [] };
        } else {
          remoteBookshelf = {
            saved: localEntries.filter((entry) => entry.collection === "saved").map((entry) => entry.book),
            favorites: localEntries.filter((entry) => entry.collection === "favorites").map((entry) => entry.book),
          };
        }
      }
      if (revision === state.bookshelfRevision) {
        state.bookshelf.saved = Array.isArray(remoteBookshelf.saved) ? remoteBookshelf.saved : [];
        state.bookshelf.favorites = Array.isArray(remoteBookshelf.favorites) ? remoteBookshelf.favorites : [];
      }
      state.bookshelfLimit = Number(response.data?.limit) || 100;
      state.bookshelfLoaded = true;
      state.bookshelfLastLoadedAt = Date.now();
      persistBookshelfCache();
    } catch (error) {
      state.bookshelfError = error.message === "rate_limited"
        ? "Please wait a moment before refreshing your bookshelf."
        : error.message === "bookshelf_full"
          ? `This device has more than ${state.bookshelfLimit} saved books. Remove some, then reopen My Bookshelf to sync them.`
          : "Couldn’t sync your bookshelf. Showing the latest copy saved on this device.";
    } finally {
      state.bookshelfLoading = false;
      if (state.currentPage === "bookshelf") renderBookshelf();
    }
  }

  async function sendBookshelfMutation(payload) {
    if (!state.bookshelfCloudEnabled) return true;
    try {
      await api("bookshelf", { method: "POST", body: JSON.stringify(payload) });
      state.bookshelfRevision += 1;
      state.bookshelfLastLoadedAt = Date.now();
      return true;
    } catch (error) {
      const message = error.message === "bookshelf_full"
        ? `Your bookshelf has reached its ${state.bookshelfLimit}-book limit. Remove a book before adding another.`
        : error.message === "rate_limited"
          ? "You’re making changes too quickly. Please wait a moment and try again."
          : "Couldn’t sync that change. Please check your connection and try again.";
      if (typeof webApp?.showAlert === "function") webApp.showAlert(message);
      else window.alert(message);
      return false;
    }
  }

  function bookCacheId(book) {
    const isbn = String(book.isbn || book.isbn13 || book.isbn_10 || "").replace(/[^0-9X]/gi, "").toLowerCase();
    if (isbn) return `isbn:${isbn}`;
    const key = (value) => String(value || "").normalize("NFKC").toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
    return `book:${key(book.title)}|${key(book.author)}`;
  }

  function shelfBook(book, addedAt = Date.now()) {
    return {
      id: bookCacheId(book), title: String(book.title || "Untitled"), author: String(book.author || "Unknown author"),
      cover_url: safeHttpUrl(book.cover_url), categories: Array.isArray(book.categories) ? book.categories.slice(0, 12) : [],
      rating: book.rating ?? null, rating_count: book.rating_count ?? null, published_date: book.published_date || "",
      page_count: book.page_count || "", language: book.language || "", isbn: book.isbn || book.isbn13 || book.isbn_10 || "", source: book.source || "",
      addedAt, description: String(book.description || "").slice(0, 5000), metadata_source: book.metadata_source || "",
      translated_title: book.translated_title || "", translated_description: String(book.translated_description || "").slice(0, 5000),
      title_needs_translation: Boolean(book.title_needs_translation), description_needs_translation: Boolean(book.description_needs_translation),
      details_checked: Boolean(book.details_checked),
      show_translation: Boolean(book.show_translation), info_link: safeHttpUrl(book.info_link),
    };
  }

  function refreshCachedBook(book) {
    const id = bookCacheId(book);
    let changed = false;
    for (const collection of ["saved", "favorites"]) {
      const index = state.bookshelf[collection].findIndex((item) => item.id === id
        || (String(item.title).toLocaleLowerCase() === String(book.title || "").toLocaleLowerCase()
          && String(item.author).toLocaleLowerCase() === String(book.author || "").toLocaleLowerCase()));
      if (index >= 0) {
        const existing = state.bookshelf[collection][index];
        state.bookshelf[collection][index] = { ...shelfBook(book, existing.addedAt), id: existing.id };
        changed = true;
      }
    }
    if (changed) persistBookshelfCache();
  }

  function shelfContains(collection, id) { return state.bookshelf[collection].some((book) => book.id === id); }

  function confirmBookshelfAction({ title, copy, accept = "Confirm" }) {
    const dialog = elements.bookshelfConfirm;
    dialog.querySelector("#bookshelf-confirm-title").textContent = title;
    dialog.querySelector("#bookshelf-confirm-copy").textContent = copy;
    const acceptButton = dialog.querySelector("#bookshelf-confirm-accept");
    acceptButton.textContent = accept;
    if (typeof dialog.showModal !== "function") return Promise.resolve(window.confirm(`${title}\n\n${copy}`));
    return new Promise((resolve) => {
      const finish = (value) => {
        dialog.close();
        dialog.removeEventListener("close", onClose);
        resolve(value);
      };
      const onClose = () => finish(false);
      dialog.addEventListener("close", onClose, { once: true });
      dialog.querySelector("#bookshelf-confirm-cancel").onclick = () => finish(false);
      acceptButton.onclick = () => finish(true);
      dialog.showModal();
    });
  }

  async function addToCollection(collection, book) {
    const cached = shelfBook(book);
    const collectionName = collection === "favorites" ? "your favourites" : "your bookshelf";
    const alreadyAdded = shelfContains(collection, cached.id);
    const otherCollection = collection === "favorites" ? "saved" : "favorites";
    const moving = !alreadyAdded && shelfContains(otherCollection, cached.id);
    const otherName = otherCollection === "saved" ? "My Books" : "Favourites";
    const targetName = collection === "favorites" ? "Favourites" : "My Books";
    if (!alreadyAdded && state.bookshelfCloudEnabled && !state.bookshelfLoaded) {
      if (typeof webApp?.showAlert === "function") webApp.showAlert("Your bookshelf is still syncing. Please wait before adding or moving a book.");
      else window.alert("Your bookshelf is still syncing. Please wait before adding or moving a book.");
      return;
    }
    if (!alreadyAdded && !moving && state.bookshelf.saved.length + state.bookshelf.favorites.length >= state.bookshelfLimit) {
      const message = `Your bookshelf has reached its ${state.bookshelfLimit}-book limit. Remove a book before adding another.`;
      if (typeof webApp?.showAlert === "function") webApp.showAlert(message);
      else window.alert(message);
      return;
    }
    const accepted = await confirmBookshelfAction({
      title: alreadyAdded ? "Remove this book?" : moving ? `Move to ${targetName}?` : collection === "favorites" ? "Add to favourites?" : "Add to your bookshelf?",
      copy: alreadyAdded
        ? `Remove “${cached.title}” from ${collectionName}?`
        : moving
          ? `“${cached.title}” will move from ${otherName} to ${targetName}, so it appears in only one section.`
          : `${collection === "favorites" ? "Save" : "Add"} “${cached.title}” ${collection === "favorites" ? "to your favourites" : "to your bookshelf"}?`,
      accept: alreadyAdded ? "Remove" : moving ? `Move to ${targetName}` : collection === "favorites" ? "Add favourite" : "Add book",
    });
    if (!accepted) return;
    const actionSucceeded = await sendBookshelfMutation(alreadyAdded
      ? { action: "remove", collection, book_key: cached.id }
      : { action: "upsert", collection, book: cached });
    if (!actionSucceeded) return;
    if (alreadyAdded) state.bookshelf[collection] = state.bookshelf[collection].filter((item) => item.id !== cached.id);
    else {
      if (moving) state.bookshelf[otherCollection] = state.bookshelf[otherCollection].filter((item) => item.id !== cached.id);
      state.bookshelf[collection] = [cached, ...state.bookshelf[collection]];
    }
    persistBookshelfCache();
    if (state.currentPage === "bookshelf") renderBookshelf();
    if (state.currentDetailBook) renderBookDetails(state.currentDetailBook, state.detailLoadingMessage, state.activeDetailContext);
    webApp?.HapticFeedback?.notificationOccurred(alreadyAdded ? "warning" : "success");
    if (!alreadyAdded && collection === "saved" && state.bookshelfAddMode) {
      state.bookshelfAddMode = false;
      if (elements.dialog.open) elements.dialog.close();
      showBookshelfPage();
    }
  }

  function beginBookshelfSearch() {
    state.bookshelfAddMode = true;
    elements.input.value = "";
    state.query = "";
    resetResultsForInput("");
    showHomePage({ restoreScroll: false, searchFocused: true });
    elements.input.focus();
    showStartupMessage("Search for a book, open its details, then choose Add to My Bookshelf.", "info");
  }

  function renderBookshelf() {
    const root = elements.bookshelf;
    const activeTab = root.dataset.activeTab || "saved";
    root.replaceChildren();
    const header = node("header", "bookshelf-header");
    const headingCopy = node("div", "library-heading-copy");
    headingCopy.append(node("p", "eyebrow", "YOUR COLLECTION"));
    const title = node("h1", "bookshelf-title", "My Bookshelf");
    title.id = "bookshelf-title";
    headingCopy.append(title);
    const subtitle = node("p", "bookshelf-subtitle", "Books worth keeping close.");
    headingCopy.append(subtitle);
    const headingGroup = node("div", "library-title-group");
    headingGroup.append(makePageBackButton("previous page"), headingCopy);
    header.append(headingGroup);
    const addButton = node("button", "bookshelf-add-button library-add-button");
    addButton.type = "button";
    addButton.setAttribute("aria-label", "Search and add a book");
    addButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg><span>Add books</span>';
    addButton.addEventListener("click", beginBookshelfSearch);
    header.append(addButton);
    root.append(header);

    const tabs = node("div", "bookshelf-tabs");
    tabs.setAttribute("role", "tablist");
    [["saved", "My Books", "▤"], ["favorites", "Favourites", "♡"]].forEach(([key, label, icon]) => {
      const button = node("button", `bookshelf-tab${activeTab === key ? " active" : ""}`);
      button.type = "button";
      button.id = `bookshelf-tab-${key}`;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", String(activeTab === key));
      button.setAttribute("aria-controls", "bookshelf-panel");
      button.append(node("span", "bookshelf-tab-icon", icon), document.createTextNode(label), node("span", "bookshelf-tab-count", String(state.bookshelf[key].length)));
      button.addEventListener("click", () => { root.dataset.activeTab = key; renderBookshelf(); });
      tabs.append(button);
    });
    root.append(tabs);

    const panel = node("section", "bookshelf-panel");
    panel.id = "bookshelf-panel";
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", `bookshelf-tab-${activeTab}`);
    if (state.bookshelfLoading && !state.bookshelfLoaded) {
      panel.append(renderBookLoader("Syncing your bookshelf…", "bookshelf-sync-loader"));
      root.append(panel);
      return;
    }
    if (state.bookshelfError) panel.append(node("p", "bookshelf-sync-note", state.bookshelfError));
    const ui = state.bookshelfUi[activeTab];
    const controls = node("div", "bookshelf-controls");
    const searchWrap = node("label", "bookshelf-search-wrap");
    searchWrap.append(node("span", "bookshelf-search-icon", "⌕"));
    const searchInput = node("input", "bookshelf-search");
    searchInput.type = "search";
    searchInput.placeholder = activeTab === "saved" ? "Find in My Books…" : "Find in Favourites…";
    searchInput.value = ui.query;
    searchInput.setAttribute("aria-label", searchInput.placeholder);
    searchWrap.append(searchInput);
    controls.append(searchWrap);

    const sortOptions = [["newest", "Recently added"], ["oldest", "Oldest added"], ["title-asc", "Title A–Z"], ["title-desc", "Title Z–A"]];
    const sortWrap = node("div", "bookshelf-sort-wrap");
    const sort = node("button", "bookshelf-sort", sortOptions.find(([value]) => value === ui.sort)?.[1] || "Recently added");
    sort.type = "button";
    sort.setAttribute("aria-haspopup", "listbox");
    sort.setAttribute("aria-expanded", "false");
    sort.innerHTML = `${sort.textContent}<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4"/></svg>`;
    const sortMenu = node("div", "bookshelf-sort-menu");
    sortMenu.setAttribute("role", "listbox");
    sortMenu.setAttribute("aria-label", "Sort books");
    sortMenu.hidden = true;
    sortOptions.forEach(([value, label]) => {
      const option = node("button", `bookshelf-sort-option${ui.sort === value ? " active" : ""}`, label);
      option.type = "button";
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", String(ui.sort === value));
      option.addEventListener("click", () => { ui.sort = value; renderBookshelf(); });
      sortMenu.append(option);
    });
    sort.addEventListener("click", () => { const open = sortMenu.hidden; sortMenu.hidden = !open; sort.setAttribute("aria-expanded", String(open)); });
    sortWrap.append(sort, sortMenu);
    controls.append(sortWrap);
    const viewToggle = node("div", "bookshelf-view-toggle");
    viewToggle.setAttribute("role", "group");
    viewToggle.setAttribute("aria-label", "Book layout");
    [["grid", "▦", "Grid view"], ["list", "☷", "List view"]].forEach(([view, icon, label]) => {
      const button = node("button", `bookshelf-view-button${ui.view === view ? " active" : ""}`, icon);
      button.type = "button"; button.title = label; button.setAttribute("aria-label", label); button.setAttribute("aria-pressed", String(ui.view === view));
      button.addEventListener("click", () => { ui.view = view; renderBookshelf(); });
      viewToggle.append(button);
    });
    controls.append(viewToggle);

    const selectionRow = node("div", "bookshelf-selection-row");
    const selectionLabel = node("span", "bookshelf-selection-count", ui.selecting ? `${ui.selected.length} selected` : `${state.bookshelf[activeTab].length} ${state.bookshelf[activeTab].length === 1 ? "book" : "books"}`);
    selectionRow.append(selectionLabel);
    const selectButton = node("button", "bookshelf-text-button", ui.selecting ? "Cancel selection" : "Select");
    selectButton.type = "button";
    selectButton.addEventListener("click", () => { ui.selecting = !ui.selecting; ui.selected = []; renderBookshelf(); });
    selectionRow.append(selectButton);
    if (ui.selecting) {
      const removeSelected = node("button", "bookshelf-remove-selected", "Remove selected");
      removeSelected.type = "button"; removeSelected.disabled = !ui.selected.length;
      removeSelected.addEventListener("click", async () => {
        const count = ui.selected.length;
        if (!count) return;
        const accepted = await confirmBookshelfAction({ title: `Remove ${count} ${count === 1 ? "book" : "books"}?`, copy: `This will remove the selected ${activeTab === "saved" ? "books from your bookshelf" : "books from your favourites"}.`, accept: `Remove ${count}` });
        if (!accepted) return;
        const removedRemotely = await sendBookshelfMutation({ action: "remove_many", collection: activeTab, book_keys: ui.selected });
        if (!removedRemotely) return;
        state.bookshelf[activeTab] = state.bookshelf[activeTab].filter((book) => !ui.selected.includes(book.id));
        ui.selected = []; ui.selecting = false; persistBookshelfCache(); renderBookshelf();
      });
      selectionRow.append(removeSelected);
    }
    panel.append(controls, selectionRow);
    const searchNeedle = ui.query.trim().toLocaleLowerCase();
    const books = state.bookshelf[activeTab].filter((book) => !searchNeedle || `${book.title} ${book.author}`.toLocaleLowerCase().includes(searchNeedle));
    books.sort((a, b) => ui.sort === "oldest" ? a.addedAt - b.addedAt
      : ui.sort === "title-asc" ? a.title.localeCompare(b.title)
        : ui.sort === "title-desc" ? b.title.localeCompare(a.title) : b.addedAt - a.addedAt);
    if (!books.length) {
      const empty = node("div", "bookshelf-empty");
      empty.append(node("span", "bookshelf-empty-mark", activeTab === "saved" ? "▤" : "♡"));
      empty.append(node("h2", "bookshelf-empty-title", searchNeedle ? "No matches found" : activeTab === "saved" ? "Your shelf is waiting" : "Save a book you love"));
      empty.append(node("p", "bookshelf-empty-copy", searchNeedle ? "Try another title or author." : activeTab === "saved" ? "Add books you want to find again, all in one lovely place." : "Tap the Like button on a book’s details page to save it here."));
      if (!searchNeedle && activeTab === "saved") {
        const emptyAdd = node("button", "bookshelf-add-button empty-add", "Find a book to add");
        emptyAdd.type = "button"; emptyAdd.addEventListener("click", beginBookshelfSearch); empty.append(emptyAdd);
      }
      panel.append(empty);
    } else {
      const list = node("div", `bookshelf-books ${ui.view === "list" ? "list-view" : "grid-view"}`);
      books.forEach((book) => {
        const card = node("article", `bookshelf-book-card${activeTab === "saved" ? " has-favorite-action" : ""}${ui.selecting ? " is-selecting" : ""}${ui.selected.includes(book.id) ? " selected" : ""}`);
        if (ui.selecting) {
          const check = node("button", "bookshelf-select-book", ui.selected.includes(book.id) ? "✓" : "");
          check.type = "button"; check.setAttribute("aria-label", `${ui.selected.includes(book.id) ? "Deselect" : "Select"} ${book.title}`);
          check.addEventListener("click", () => { ui.selected = ui.selected.includes(book.id) ? ui.selected.filter((id) => id !== book.id) : [...ui.selected, book.id]; renderBookshelf(); });
          card.append(check);
        }
        const open = node("button", "bookshelf-book-open"); open.type = "button"; open.setAttribute("aria-label", `Open details for ${book.title}`);
        const coverWrap = node("span", "bookshelf-book-cover"); appendCover(coverWrap, book.cover_url, book.title, "bookshelf-cover");
        const info = node("span", "bookshelf-book-info"); info.append(node("strong", "bookshelf-book-title", book.title), node("span", "bookshelf-book-author", book.author));
        if (book.categories?.length) info.append(node("span", "bookshelf-book-genre", book.categories.slice(0, 2).join(" · ")));
        open.append(coverWrap, info);
        open.addEventListener("click", () => {
          if (ui.selecting) {
            ui.selected = ui.selected.includes(book.id) ? ui.selected.filter((id) => id !== book.id) : [...ui.selected, book.id];
            renderBookshelf();
            return;
          }
          openDetails(book, { collection: "bookshelf", book: { ...book } });
        });
        card.append(open);
        const gridActions = ui.view === "grid" ? node("div", "bookshelf-card-actions") : null;
        if (activeTab === "saved" && !ui.selecting) {
          const favorite = node("button", "bookshelf-favorite-book");
          favorite.type = "button";
          favorite.title = "Move to Favourites";
          favorite.setAttribute("aria-label", `Move ${book.title} to Favourites`);
          favorite.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.8 8.8c0 5.1-8.8 10.2-8.8 10.2S3.2 13.9 3.2 8.8A4.4 4.4 0 0 1 12 6.6a4.4 4.4 0 0 1 8.8 2.2Z"/></svg>';
          favorite.addEventListener("click", () => void addToCollection("favorites", book));
          (gridActions || card).append(favorite);
        }
        const remove = node("button", "bookshelf-remove-book");
        remove.type = "button"; remove.title = activeTab === "favorites" ? "Remove from favourites" : "Remove from My Books"; remove.setAttribute("aria-label", remove.title);
        remove.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4.5 6h11m-9.5 0 .6 10.5h7.8L15.5 6M8 6V4h4v2m-3 3v5m2-5v5"/></svg>';
        remove.addEventListener("click", async () => {
          const accepted = await confirmBookshelfAction({ title: activeTab === "favorites" ? "Remove favourite?" : "Remove from My Books?", copy: `“${book.title}” will be removed from ${activeTab === "favorites" ? "your favourites" : "your bookshelf"}.`, accept: "Remove" });
          if (!accepted) return;
          const removedRemotely = await sendBookshelfMutation({ action: "remove", collection: activeTab, book_key: book.id });
          if (!removedRemotely) return;
          state.bookshelf[activeTab] = state.bookshelf[activeTab].filter((item) => item.id !== book.id); persistBookshelfCache(); renderBookshelf();
        });
        (gridActions || card).append(remove);
        if (gridActions) card.append(gridActions);
        list.append(card);
      });
      panel.append(list);
    }
    root.append(panel);
    searchInput.addEventListener("input", () => { ui.query = searchInput.value; const position = searchInput.selectionStart; renderBookshelf(); const replacement = root.querySelector(".bookshelf-search"); replacement.focus(); replacement.setSelectionRange(position, position); });
  }

  function toggleQuickMenu(forceOpen) {
    if (["recommendations", "bookshelf"].includes(state.currentPage)) {
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
        ...(state.inlineSessionToken ? { "X-Annie-Inline-Session": state.inlineSessionToken } : {}),
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
    if (error.status === 401) return "Telegram could not verify this session. Close and reopen the Mini App from Telegram.";
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
    const loader = node("div", `recommendation-loading ${variant || ""}`.trim());
    loader.setAttribute("role", "status");
    loader.setAttribute("aria-live", "polite");
    const books = node("span", "recommendation-loading-books");
    books.setAttribute("aria-hidden", "true");
    for (let index = 0; index < 4; index += 1) books.append(node("span", "recommendation-loading-book"));
    loader.append(books, node("span", "recommendation-loading-copy", message));
    return loader;
  }

  function renderGenreTabs() {
    const genres = ["All", "Fantasy", "Romance", "Mystery", "Thriller", "Sci-Fi", "Horror", "Classics", "Biography"];
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

  function renderHomeShelf(books, query) {
    elements.featuredBookSlot.replaceChildren();
    elements.trendingBooks.replaceChildren();
    elements.moreTrendingBooks.replaceChildren();
    const [featuredBook, ...remainingBooks] = books;
    if (!featuredBook) {
      elements.featuredBookSlot.hidden = true;
      elements.moreSection.hidden = true;
      return;
    }

    const featuredCard = renderBookCard(featuredBook, 0, { query, page: 1, collection: "featured" });
    featuredCard.classList.add("featured-book-card");
    const copy = featuredCard.querySelector(".book-copy");
    copy.prepend(node("span", "featured-book-kicker", "A place to start"));
    const summary = String(featuredBook.description || "").replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
    if (summary) {
      const shortSummary = summary.length > 148 ? `${summary.slice(0, 145).replace(/\s+\S*$/, "")}…` : summary;
      copy.insertBefore(node("p", "featured-book-summary", shortSummary), copy.querySelector(".book-categories"));
    }
    elements.featuredBookSlot.append(featuredCard);
    elements.featuredBookSlot.hidden = false;

    remainingBooks.slice(0, HOME_SHELF_SIZE - 1).forEach((book, index) => {
      const absoluteIndex = index + 1;
      elements.trendingBooks.append(renderBookCard(book, absoluteIndex, {
        query, page: Math.floor(absoluteIndex / 5) + 1, collection: "featured",
      }));
    });
    books.slice(HOME_SHELF_SIZE).forEach((book, index) => {
      const absoluteIndex = index + HOME_SHELF_SIZE;
      elements.moreTrendingBooks.append(renderBookCard(book, absoluteIndex, {
        query, page: Math.floor(absoluteIndex / 5) + 1, collection: "more",
      }));
    });
    elements.moreSection.hidden = books.length <= HOME_SHELF_SIZE;
    refreshDesktopScrollControls(elements.trendingBooks);
  }

  async function loadTrending(genre) {
    const requestId = ++state.featuredRequestId;
    state.featuredBooks = [];
    elements.featuredBookSlot.replaceChildren();
    elements.featuredBookSlot.hidden = true;
    elements.discoverTitle.textContent = genre === "All" ? "Top books" : `Top ${genre} books`;
    elements.trendingBooks.replaceChildren();
    elements.moreTrendingBooks.replaceChildren();
    elements.moreSection.hidden = true;
    const cacheKey = `${HOME_TRENDING_CACHE_VERSION}:${genre}`;
    const cached = state.featuredCache[cacheKey];
    const cacheIsFresh = cached && Date.now() - cached.loadedAt < HOME_TRENDING_CACHE_MS;
    if (cached && !cacheIsFresh) {
      delete state.featuredCache[cacheKey];
    }
    if (cacheIsFresh) {
      state.featuredBooks = cached.books;
      state.featuredQuery = cached.query;
      renderHomeShelf(cached.books, cached.query);
      setDiscoverMessage(cached.books.length ? "" : "No picks found for this genre yet.");
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
      state.featuredCache[cacheKey] = {
        query: state.featuredQuery,
        books: state.featuredBooks,
        loadedAt: Date.now(),
      };
      setDiscoverMessage("");
      renderHomeShelf(state.featuredBooks, state.featuredQuery);
    } catch (error) {
      if (requestId !== state.featuredRequestId || error.name === "AbortError") return;
      elements.trendingBooks.replaceChildren();
      elements.featuredBookSlot.replaceChildren();
      elements.featuredBookSlot.hidden = true;
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
    const detailActions = node("div", "detail-book-actions");
    detailActions.append(shareButton);
    const favoriteButton = node("button", `favorite-book-button${shelfContains("favorites", bookCacheId(book)) ? " is-favorite" : ""}`);
    favoriteButton.type = "button";
    favoriteButton.setAttribute("aria-label", shelfContains("favorites", bookCacheId(book)) ? "Unlike this book" : "Like this book");
    favoriteButton.title = favoriteButton.getAttribute("aria-label");
    favoriteButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.8 8.8c0 5.1-8.8 10.2-8.8 10.2S3.2 13.9 3.2 8.8A4.4 4.4 0 0 1 12 6.6a4.4 4.4 0 0 1 8.8 2.2Z"/></svg>';
    favoriteButton.addEventListener("click", () => void addToCollection("favorites", book));
    detailActions.append(favoriteButton);
    const saved = shelfContains("saved", bookCacheId(book));
    const saveButton = node("button", `detail-save-book${saved ? " is-saved" : ""}`, saved ? "Remove from Bookshelf" : "Add to Bookshelf");
    saveButton.type = "button";
    saveButton.addEventListener("click", () => void addToCollection("saved", book));
    titleRow.append(detailActions);
    hero.append(titleRow);
    if (book.show_translation && book.translated_title
      && String(book.translated_title).toLocaleLowerCase() !== String(book.title || "").toLocaleLowerCase()) {
      hero.append(node("p", "detail-translated-title", `English title: ${book.translated_title}`));
    }
    const authorRow = node("div", "detail-author-row");
    authorRow.append(node("p", "detail-author", book.author || "Unknown author"), saveButton);
    hero.append(authorRow);
    hero.append(makeRatingDisplay(book, "detail-rating"));
    if (loadingMessage) hero.append(renderBookLoader(loadingMessage, "detail-loader"));
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
      ? "Loading complete book details…"
      : context?.collection === "bookshelf" && !book.details_checked && (!book.description || !book.isbn)
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
    } else if (context?.collection === "bookshelf" && !book.details_checked && (!book.description || !book.isbn)) {
      void fetchRecommendationBookDetails(context);
    } else if (context && context.collection !== "bookshelf") {
      void fetchGoogleBookDetails(context);
    } else if (context?.collection === "bookshelf") {
      void fetchRelatedBooks(context, book);
    }
  }

  function renderResults(data) {
    state.page = data.page;
    state.pageSize = data.page_size;
    state.total = data.total;
    state.hasMore = data.has_more;
    updateAppTabbar();
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
      refreshCachedBook(response.data.book);
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
        body: JSON.stringify({ book: {
          ...context.book,
          ...(context.collection === "bookshelf" ? { source: "" } : {}),
        } }),
        signal: controller.signal,
      });
      if (requestId !== state.detailsRequestId || state.activeDetailContext !== context || !elements.dialog.open) return;
      const enriched = response.data.book || context.book;
      if (context.collection === "bookshelf") enriched.details_checked = true;
      for (const field of ["cover_url", "rating", "rating_count", "description", "categories", "isbn", "page_count", "published_date", "language", "info_link"]) {
        const value = enriched[field];
        if ((value === undefined || value === null || value === "" || (Array.isArray(value) && !value.length)) && context.book[field] !== undefined) {
          enriched[field] = context.book[field];
        }
      }
      state.currentDetailBook = enriched;
      refreshCachedBook(enriched);
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
    if (query) state.searchFocused = true;
    updateAppTabbar();
    state.page = 1;
    state.total = 0;
    state.hasMore = false;
    state.visibleBooks = [];
    document.body.classList.toggle("search-active", Boolean(query));
    document.body.classList.toggle("search-focused", state.searchFocused);
    elements.results.replaceChildren();
    elements.pagination.hidden = true;
    elements.moreSection.hidden = true;
    if (!query) {
      elements.resultsSection.hidden = true;
      elements.discover.hidden = state.searchFocused;
      elements.welcome.hidden = !state.searchFocused;
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
    showMessage(
      query.length < 3 ? "Type at least 3 characters to search." : "Searching when you pause typing…",
      query.length < 3 ? "" : "loading",
    );
    setBusy(false);
  }

  async function initialize() {
    loadBookshelfCache();
    setBusy(true);
    if (webApp) {
      webApp.ready();
      webApp.expand();
      try { webApp.setHeaderColor?.(THEME_COLORS[document.documentElement.dataset.theme] || THEME_COLORS.purple); } catch (_) { /* Older Telegram clients */ }
      try { webApp.setBackgroundColor?.(THEME_COLORS[document.documentElement.dataset.theme] || THEME_COLORS.purple); } catch (_) { /* Older Telegram clients */ }
    }

    state.initData = webApp?.initData || "";
    const launchParams = new URLSearchParams(window.location.search);
    const inlineTicket = launchParams.get("inline_ticket") || "";
    if (!state.initData && !inlineTicket) {
      showStartupMessage("Open this page inside Telegram from your bot to search books.", "error");
      dismissLaunchWelcome();
      return;
    }

    let startupStage = "session-authentication";
    try {
      let response;
      if (inlineTicket) {
        startupStage = "inline-session-exchange";
        try {
          response = await api("inline-session", {
            method: "POST",
            body: JSON.stringify({ ticket: inlineTicket }),
          });
          state.inlineSessionToken = response.session_token || "";
          if (!state.inlineSessionToken) throw new Error("inline_session_unavailable");
        } catch (ticketError) {
          // Older start-menu buttons can contain an expired one-use ticket.
          // Fall back to Telegram's signed launch data when it is still valid.
          if (!state.initData) throw ticketError;
          response = await api("session");
        }
        startupStage = "inline-ticket-url-cleanup";
        launchParams.delete("inline_ticket");
        const cleanQuery = launchParams.toString();
        // Telegram clients can restrict history changes in some Mini App
        // launch contexts. Removing the ticket from the address bar is only
        // cosmetic; it must not prevent the authenticated app from starting.
        try {
          window.history.replaceState(
            window.history.state,
            "",
            `${window.location.pathname}${cleanQuery ? `?${cleanQuery}` : ""}${window.location.hash}`,
          );
        } catch (_) { /* Keep going with the ticket in the URL if cleanup is blocked. */ }
      } else {
        response = await api("session");
      }
      startupStage = "apply-session-response";
      state.bookshelfCloudEnabled = Boolean(response.bookshelf_enabled);
      state.bookshelfLimit = Number(response.bookshelf_limit) || 100;
      setBookshelfCacheUser(response.user.id);
      state.ready = true;
      showStartupMessage("");
      elements.greeting.textContent = response.user.first_name
        ? `Hi, ${response.user.first_name}`
        : "Hi there";
      setBusy(false);
      startupStage = "render-navigation";
      renderGenreTabs();
      startupStage = "route-initial-page";
      const startParam = webApp?.initDataUnsafe?.start_param
        || new URLSearchParams(window.location.search).get("tgWebAppStartParam")
        || "";
      const launchParams = new URLSearchParams(window.location.search);
      const page = launchParams.get("page") || ({
        recom: "recommendations", bookshelf: "bookshelf", favorites: "favorites",
      }[startParam] || "");
      if (page === "recommendations") {
        showRecommendationsPage();
        dismissLaunchWelcome();
        return;
      }
      if (page === "bookshelf" || page === "favorites") {
        elements.bookshelf.dataset.activeTab = page === "favorites" ? "favorites" : "saved";
        showBookshelfPage();
        dismissLaunchWelcome();
        return;
      }
      startupStage = "load-trending";
      await loadTrending("All");
    } catch (error) {
      if (inlineTicket && state.inlineSessionToken) {
        const diagnostic = {
          stage: startupStage,
          name: String(error?.name || "Error").slice(0, 80),
          message: String(error?.message || error || "unknown_error").slice(0, 240),
        };
        console.error("Mini App inline startup failed", diagnostic);
        try {
          await api("client-diagnostic", {
            method: "POST",
            body: JSON.stringify(diagnostic),
          });
        } catch (_) { /* Diagnostics must not replace the original startup error. */ }
      }
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
  elements.quickMenuItems.querySelector('[data-page="bookshelf"]').addEventListener("click", showBookshelfPage);
  elements.appTabbar.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-app-tab]");
    if (!tab) return;
    if (tab.dataset.appTab === "discover") {
      if (state.query || elements.input.value) {
        elements.input.value = "";
        resetResultsForInput("");
      }
      showHomePage({ restoreScroll: false });
      window.scrollTo({ top: 0, behavior: "smooth" });
    } else if (tab.dataset.appTab === "search") {
      navigateToSearch();
    } else if (tab.dataset.appTab === "recommendations") {
      showRecommendationsPage();
    } else if (tab.dataset.appTab === "bookshelf") {
      showBookshelfPage();
    }
  });
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
  elements.input.addEventListener("focus", () => {
    if (state.currentPage !== "home") return;
    state.searchFocused = true;
    document.body.classList.add("search-focused");
    if (!state.query) {
      elements.discover.hidden = true;
      elements.resultsSection.hidden = true;
      elements.welcome.hidden = false;
    }
    updateAppTabbar();
  });
  elements.previous.addEventListener("click", () => {
    if (state.page > 1) search(state.query, state.page - 1);
  });
  elements.next.addEventListener("click", () => {
    if (state.hasMore) search(state.query, state.page + 1);
  });
  elements.closeDialog.addEventListener("click", () => elements.dialog.close());
  elements.homeButton.addEventListener("click", () => elements.dialog.close());
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
    state.bookshelfAddMode = false;
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
  }, { passive: true });
  updateAppTabbar();
  initialize();
})();
