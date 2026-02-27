// Fantasy Baseball Draft Assistant - Frontend JavaScript
//
// Enhanced fetchAPI Features:
// - Circuit Breaker Pattern: Prevents cascading failures by temporarily disabling requests after consecutive failures
// - Retry Logic: Implements exponential backoff for network errors and 5xx server errors
// - Improved Error Handling: Better error messages with special handling for authentication errors
// - Request/Response Logging: Enable DEBUG=true for development logging
// - Timeout Management: 10-second timeout for all requests
// - Request Headers: Adds API version and client identification headers

const API_BASE = '/api/v1';

// Debug mode - set to true for development logging
const DEBUG = false;

// State
let players = [];
let recommendations = {};
let scarcityData = null;
let categoryPlanner = null;
let valueClassifications = {};  // Sleeper/bust classifications by player ID
let surplusValues = {};         // VORP surplus values by player ID
let currentLeagueId = null;
let currentUserKey = null;
let selectedPositions = new Set();
let currentTeam = '';
let searchQuery = '';
let draftModeActive = false;
let draftModeInterval = null;
const DRAFT_SYNC_INTERVAL = 5000; // 5 seconds
const DRAFT_CONFIG_KEY = (leagueId) => `fbb_draft_config_${leagueId}`;
const USER_KEY_STORAGE = 'fbb_user_key';

// Draft Session State
let draftSessionId = null;        // Active session ID
let draftSessionName = null;      // Session identifier/name
let canUndo = false;              // Whether undo is available
let canRedo = false;              // Whether redo is available

// Achievement System State
let unlockedAchievements = new Set();
let sessionPickCount = 0;
let myTeamPickCount = 0;

// Achievement definitions
const ACHIEVEMENTS = {
    first_pick: { icon: '1st', name: 'First Blood', desc: 'Made your first pick' },
    first_sp: { icon: 'SP', name: 'Ace Acquired', desc: 'Drafted a starting pitcher' },
    first_rp: { icon: 'RP', name: 'Closer Time', desc: 'Drafted a relief pitcher' },
    five_picks: { icon: '5', name: 'Getting Started', desc: 'Made 5 picks' },
    ten_picks: { icon: '10', name: 'Building the Squad', desc: 'Made 10 picks' },
    power_hitter: { icon: 'HR', name: 'Power Surge', desc: 'Drafted a 30+ HR hitter' },
    speed_demon: { icon: 'SB', name: 'Speed Demon', desc: 'Drafted a 20+ SB player' },
    sleeper_pick: { icon: 'SLEEPER', name: 'Hidden Gem', desc: 'Drafted a sleeper pick' },
};

// Draft Order State
let draftNumTeams = 12;
let draftUserPosition = 1;
let draftCurrentPick = 1;
let draftCurrentRound = 1;
let draftTeamOnClock = 1;
let draftIsUserPick = false;
let draftTeams = [];  // Array of {id, name, draft_position, is_user_team}
let teamPickerPlayerId = null;  // Player being drafted when team picker is open

// Track collapsed sections (default all expanded)
let collapsedSections = new Set();

// Track player recommendation categories for highlighting
let playerCategories = {}; // player_id -> 'recommended' | 'safe' | 'risky' | 'needs'

// Keeper State
let keepers = [];
let keeperTeamNames = [];
let selectedKeeperPlayerId = null;
let keeperSearchTimeout = null;

// Player Comparison State
let compareSlots = [null, null]; // Holds up to 2 player IDs

// Sort State
let sortColumn = null;      // 'overall' | 'pos' | 'risk' | 'value' | null
let sortDirection = 'asc';  // 'asc' | 'desc'

// Global Loading State
let activeRequests = 0;     // Counter for active API requests
let globalLoadingElement = null; // Reference to global loading indicator

// Normalize a string for search: strip accents, lowercase, hyphens → spaces
function normalizeForSearch(str) {
    if (!str) return '';
    return str.normalize('NFD')
              .replace(/[\u0300-\u036f]/g, '')   // strip combining diacritical marks
              .toLowerCase()
              .replace(/-/g, ' ');
}

function getOrCreateUserKey() {
    try {
        const existing = localStorage.getItem(USER_KEY_STORAGE);
        if (existing && existing.length >= 6) return existing;
        const generated = `user_${Math.random().toString(36).slice(2, 10)}`;
        localStorage.setItem(USER_KEY_STORAGE, generated);
        return generated;
    } catch {
        return `user_${Math.random().toString(36).slice(2, 10)}`;
    }
}

function withUserKey(endpoint) {
    const sep = endpoint.includes('?') ? '&' : '?';
    return `${endpoint}${sep}user_key=${encodeURIComponent(currentUserKey || '')}`;
}

function setClaimedTeamBadge(teamName) {
    const pill = document.getElementById('team-name-pill');
    const label = document.getElementById('team-name');
    if (!pill || !label) return;

    if (teamName) {
        label.textContent = teamName;
        pill.classList.remove('hidden');
    } else {
        label.textContent = '--';
        pill.classList.add('hidden');
    }
}

async function refreshClaimedTeamBadge() {
    if (!currentLeagueId) {
        setClaimedTeamBadge(null);
        return;
    }
    try {
        const teams = await fetchAPI(withUserKey(`/leagues/${currentLeagueId}/teams`));
        const mine = (teams || []).find(team => team.claimed_by_me);
        setClaimedTeamBadge(mine?.name || null);
    } catch (error) {
        console.warn('Failed to refresh claimed team badge:', error?.message || error);
        setClaimedTeamBadge(null);
    }
}

// Helper function to escape HTML special characters
function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;')
              .replace(/</g, '&lt;')
              .replace(/>/g, '&gt;')
              .replace(/"/g, '&quot;')
              .replace(/'/g, '&#39;');
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    initApp();
    
    // Add event listeners for FV star rating tooltips
    document.addEventListener('mouseover', function(e) {
        if (e.target.closest('.fv-star-rating')) {
            const rating = e.target.closest('.fv-star-rating');
            const tooltip = rating.querySelector('.fv-tooltip');
            if (tooltip) {
                tooltip.classList.add('visible');
            }
        }
    });
    
    document.addEventListener('mouseout', function(e) {
        if (e.target.closest('.fv-star-rating')) {
            const rating = e.target.closest('.fv-star-rating');
            const tooltip = rating.querySelector('.fv-tooltip');
            if (tooltip) {
                tooltip.classList.remove('visible');
            }
        }
    });
});

// ─── Reliability helpers ─────────────────────────────────────────────────────

// Wrap render functions so a single crash doesn't blank the whole page
function safeRender(fn, sectionId, label) {
    try {
        fn();
    } catch (e) {
        console.error(`Render error in ${label}:`, e);
        const el = document.getElementById(sectionId);
        if (el) el.innerHTML = `<p class="empty-state">&#9888; Could not display ${label}. Please refresh.</p>`;
    }
}

// Insert shimmer placeholders into each loading panel
function showSkeletonLoaders() {
    const skeletonHTML = [1, 2, 3].map(() => '<div class="skeleton-pulse"></div>').join('');
    ['player-list', 'recommended-picks', 'safe-picks', 'risky-picks', 'needs-picks', 'prospect-picks', 'scarcity-dashboard', 'planner-dashboard'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.innerHTML = skeletonHTML;
    });
}

// ─────────────────────────────────────────────────────────────────────────────

async function initApp() {
    currentUserKey = getOrCreateUserKey();
    // Reset comparison state on every (re-)init / league change
    compareSlots = [null, null];
    document.getElementById('compare-tray')?.classList.remove('visible');

    // Try to load existing league or create default
    try {
        const leagues = await fetchAPI('/leagues/');
        let leagueData;
        if (leagues.length > 0) {
            leagueData = leagues[0];
        } else {
            // Create default league (configure via Mock Draft setup)
            leagueData = await fetchAPI('/leagues/', {
                method: 'POST',
                body: JSON.stringify({
                    espn_league_id: 0,
                    year: new Date().getFullYear(),
                    name: 'My Fantasy League'
                })
            });
        }
        currentLeagueId = leagueData.id;
        document.getElementById('league-name').textContent = leagueData.name;
        document.getElementById('league-name-badge').classList.toggle('hidden', leagueData.espn_league_id === 0);
        checkCredentialStatus(leagueData);

        if (leagueData.espn_league_id === 0 && !localStorage.getItem('setupSkipped')) {
            showSetupWizard(leagueData);
        }
    } catch (error) {
        console.error('Failed to initialize league:', error);
    }

    // Load initial data in parallel — a single failure won't block the others
    showSkeletonLoaders();
    await Promise.allSettled([
        loadPlayers(),
        loadRecommendations(),
        loadScarcity(),
        refreshClaimedTeamBadge(),
    ]);
    updateLastUpdated();

    // Restore collapsed section preferences
    restoreCollapsedSections();

    // Load keepers for team name pre-population in draft setup
    await loadKeepers();

    // Check for active draft session
    await checkActiveSession();

    // Initialize session persistence
    await initSessionPersistence();

    // Setup keyboard shortcuts for undo/redo
    setupKeyboardShortcuts();

    // Stick draft bar to bottom on narrow viewports
    if (window.innerWidth <= 900) {
        document.getElementById('draft-session-bar')?.classList.add('mobile-bottom');
    }
}

// Circuit Breaker Pattern Implementation
// Prevents cascading failures by temporarily disabling requests after consecutive failures
// States: CLOSED (normal operation) -> OPEN (requests blocked) -> HALF_OPEN (testing recovery)
class CircuitBreaker {
    constructor(failureThreshold = 5, cooldownPeriod = 60000) {
        this.failureThreshold = failureThreshold;
        this.cooldownPeriod = cooldownPeriod;
        this.failureCount = 0;
        this.lastFailureTime = null;
        this.state = 'CLOSED'; // CLOSED, OPEN, HALF_OPEN
    }

    canExecute() {
        if (this.state === 'OPEN') {
            const now = Date.now();
            if (now - this.lastFailureTime >= this.cooldownPeriod) {
                this.state = 'HALF_OPEN';
                return true;
            }
            return false;
        }
        return true;
    }

    onSuccess() {
        this.failureCount = 0;
        this.state = 'CLOSED';
    }

    onFailure() {
        this.failureCount++;
        this.lastFailureTime = Date.now();

        if (this.failureCount >= this.failureThreshold) {
            this.state = 'OPEN';
        }
    }

    getState() {
        return this.state;
    }
}

// Global circuit breaker instance
const globalCircuitBreaker = new CircuitBreaker(5, 60000); // 5 failures, 60 second cooldown

// Enhanced API Helper with Circuit Breaker, Retry Logic, and Improved Error Handling
// Features:
// - Circuit Breaker Pattern: Prevents cascading failures
// - Exponential Backoff Retry: Retries failed requests with increasing delays
// - Improved Error Messages: Better user-facing error messages
// - Authentication Error Handling: Redirects to settings for auth errors
// - Request/Response Logging: Development logging when DEBUG=true
// - Standard Headers: Adds API version and client identification
// - Timeout Protection: 10-second timeout for all requests
async function fetchAPI(endpoint, options = {}) {
    // Increment loading counter before making request
    incrementLoadingCounter();

    try {
    const {
        timeoutMs = 10000,
        retries = 3,
        ...requestOptions
    } = options;
    const url = `${API_BASE}${endpoint}`;
    const config = {
        headers: {
            'Content-Type': 'application/json',
            'X-API-Version': '1.0',
            'X-Client-Identifier': 'FBB-Web-Client',
        },
        cache: 'no-store',
        ...requestOptions,
    };

    // Exponential backoff retry logic
    const maxRetries = Math.max(0, Number(retries) || 0);
    const baseDelay = 1000; // 1 second

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
        try {
            // Check circuit breaker state
            if (!globalCircuitBreaker.canExecute()) {
                throw new Error('Service temporarily unavailable. Please try again in a moment.');
            }

            // Add timeout
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
            config.signal = controller.signal;

            // Log request for debugging (in dev mode)
            if (typeof DEBUG !== 'undefined' && DEBUG) {
                console.log(`[API Request] ${config.method || 'GET'} ${url}`, config);
            }

            const response = await fetch(url, config);
            clearTimeout(timeoutId);

            // Log response for debugging (in dev mode)
            if (typeof DEBUG !== 'undefined' && DEBUG) {
                console.log(`[API Response] ${response.status} ${url}`);
            }

            if (!response.ok) {
                let errorMessage = `API error: ${response.status} ${response.statusText}`;

                // Try to parse error details from response
                try {
                    const errorData = await response.json();
                    if (errorData.detail) {
                        errorMessage = errorData.detail;
                    } else if (errorData.message) {
                        errorMessage = errorData.message;
                    }

                    // Special handling for authentication errors
                    if (response.status === 401 || response.status === 403) {
                        errorMessage = 'Authentication required. Please check your ESPN credentials.';
                        // Redirect to settings for authentication errors
                        setTimeout(() => {
                            showSettingsModal();
                        }, 1000);
                    }
                } catch (e) {
                    // If we can't parse JSON, use the status text
                }

                // Record failure in circuit breaker
                globalCircuitBreaker.onFailure();

                // For 5xx errors, we might want to retry
                if (response.status >= 500 && response.status < 600 && attempt < maxRetries) {
                    const delay = baseDelay * Math.pow(2, attempt);
                    console.warn(`Server error (${response.status}), retrying in ${delay}ms... (attempt ${attempt + 1}/${maxRetries + 1})`);
                    await new Promise(resolve => setTimeout(resolve, delay));
                    continue;
                }

                throw new Error(errorMessage);
            }

            // Record success in circuit breaker
            globalCircuitBreaker.onSuccess();

            return response.json();
        } catch (error) {
            // Record failure in circuit breaker for network errors
            globalCircuitBreaker.onFailure();

            // Handle different types of errors
            if (error.name === 'AbortError') {
                if (attempt < maxRetries) {
                    const delay = baseDelay * Math.pow(2, attempt);
                    console.warn(`Request timeout, retrying in ${delay}ms... (attempt ${attempt + 1}/${maxRetries + 1})`);
                    await new Promise(resolve => setTimeout(resolve, delay));
                    continue;
                }
                throw new Error('Request timed out. Please try again.');
            } else if (error instanceof TypeError && error.message.includes('fetch')) {
                if (attempt < maxRetries) {
                    const delay = baseDelay * Math.pow(2, attempt);
                    console.warn(`Network error, retrying in ${delay}ms... (attempt ${attempt + 1}/${maxRetries + 1})`);
                    await new Promise(resolve => setTimeout(resolve, delay));
                    continue;
                }
                throw new Error('Network error. Please check your connection and try again.');
            } else {
                // For other errors, don't retry unless it's a 5xx server error
                throw error;
            }
        }
    }

    // This shouldn't be reached, but just in case
    throw new Error('Maximum retry attempts exceeded.');
    } finally {
        // Decrement loading counter after request completes
        decrementLoadingCounter();
    }
}

// Load players
async function loadPlayers() {
    try {
        let endpoint = '/players/?available_only=true&limit=500';

        players = await fetchAPI(endpoint);

        // Also load value classifications and surplus values
        await Promise.all([loadValueClassifications(), loadSurplusValues()]);

        renderPlayerList();
    } catch (error) {
        console.error('Failed to load players:', error);
        showError(`Failed to load players: ${error.message}`, 'error');
    }
}

// Load sleeper/bust value classifications
async function loadValueClassifications() {
    try {
        const data = await fetchAPI('/players/value-classifications?available_only=true&limit=500');
        valueClassifications = data.classifications || {};
        console.log(`Loaded value classifications: ${data.sleepers} sleepers, ${data.bust_risks} bust risks`);
    } catch (error) {
        console.error('Failed to load value classifications:', error);
        valueClassifications = {};
    }
}

// Load VORP surplus values
async function loadSurplusValues() {
    try {
        const data = await fetchAPI('/players/surplus-values?available_only=true&limit=500');
        surplusValues = data.surplus_values || {};
        console.log(`Loaded surplus values for ${data.calculated} players`);
    } catch (error) {
        console.error('Failed to load surplus values:', error);
        surplusValues = {};
    }
}

// Render surplus value with colored text
function renderSurplusValue(playerId) {
    const data = surplusValues[playerId];
    if (!data) return '<span class="text-gray-600">--</span>';

    const surplus = data.surplus_value;
    let color, prefix;
    if (surplus >= 3.0) { color = 'text-green-400'; prefix = '+'; }
    else if (surplus >= 1.0) { color = 'text-emerald-300'; prefix = '+'; }
    else if (surplus >= 0) { color = 'text-gray-400'; prefix = '+'; }
    else if (surplus >= -2.0) { color = 'text-orange-400'; prefix = ''; }
    else { color = 'text-red-400'; prefix = ''; }

    return `<span class="${color}" title="VORP surplus at ${data.position_used}">${prefix}${surplus.toFixed(1)}</span>`;
}

// Get highlight class for player based on recommendation category
function getPlayerHighlightClass(playerId) {
    const category = playerCategories[playerId];
    switch (category) {
        case 'recommended': return 'highlight-recommended';
        case 'safe': return 'highlight-safe';
        case 'risky': return 'highlight-risky';
        case 'needs': return 'highlight-needs';
        case 'prospect': return 'highlight-prospect';
        default: return '';
    }
}

function handleSort(column) {
    if (sortColumn === column) {
        if (sortDirection === 'asc') {
            sortDirection = 'desc';
        } else {
            sortColumn = null;
            sortDirection = 'asc';
        }
    } else {
        sortColumn = column;
        sortDirection = 'asc';
    }
    updateSortIndicators();
    renderPlayerList();
}

function getSortValue(player, column) {
    switch (column) {
        case 'overall': return player.last_season_rank ?? Infinity;
        case 'pos': return player.last_season_pos_rank ?? Infinity;
        case 'risk': return player.risk_score ?? Infinity;
        case 'value': {
            const sv = surplusValues[player.id]?.surplus_value;
            return sv ?? -Infinity;
        }
        default: return 0;
    }
}

function updateSortIndicators() {
    ['overall', 'pos', 'risk', 'value'].forEach(col => {
        const el = document.getElementById('sort-' + col);
        if (el) {
            if (sortColumn === col) {
                el.textContent = sortDirection === 'asc' ? ' ▲' : ' ▼';
            } else {
                el.textContent = '';
            }
        }
    });
}

// Render player list
function renderPlayerList() {
    const tbody = document.getElementById('player-list');
    let filteredPlayers = players;

    // Apply search filter
    if (searchQuery) {
        const normQ = normalizeForSearch(searchQuery);
        filteredPlayers = filteredPlayers.filter(p =>
            normalizeForSearch(p.name).includes(normQ)
        );
    }

    // Apply team filter
    if (currentTeam) {
        filteredPlayers = filteredPlayers.filter(p =>
            p.team === currentTeam
        );
    }

    // Apply position filter
    if (selectedPositions.size > 0) {
        filteredPlayers = filteredPlayers.filter(p => {
            if (selectedPositions.has('MULTI')) {
                // Multi = batters eligible at 2+ real field positions (DH doesn't count)
                if (p.positions && (p.positions.includes('/') || p.positions.includes(','))) {
                    const posList = p.positions.split(/[\/,]/).map(s => s.trim().toUpperCase());
                    const fieldPositions = posList.filter(pos => pos !== 'DH');
                    const hasBattingPos = fieldPositions.some(pos => !['SP', 'RP'].includes(pos));
                    if (hasBattingPos && fieldPositions.length > 1) return true;
                }
            }
            if (selectedPositions.has('RP/SP')) {
                // Pitchers eligible at both RP and SP
                if (p.positions && p.positions.includes('SP') && p.positions.includes('RP')) {
                    return true;
                }
            }
            return [...selectedPositions].some(pos =>
                pos !== 'MULTI' && pos !== 'RP/SP' && p.positions && p.positions.includes(pos)
            );
        });
    }

    // Apply column sort
    if (sortColumn) {
        filteredPlayers = [...filteredPlayers].sort((a, b) => {
            let aVal = getSortValue(a, sortColumn);
            let bVal = getSortValue(b, sortColumn);
            // Nulls (Infinity/-Infinity) always sort last
            const aNull = !isFinite(aVal);
            const bNull = !isFinite(bVal);
            if (aNull && bNull) return 0;
            if (aNull) return 1;
            if (bNull) return -1;
            return sortDirection === 'asc' ? aVal - bVal : bVal - aVal;
        });
    }

    tbody.innerHTML = filteredPlayers.map((player, index) => {
        const highlightClass = getPlayerHighlightClass(player.id);
        const injuryBadge = player.is_injured
            ? `<span class="ml-2 px-1.5 py-0.5 text-xs rounded-full bg-red-500/20 text-red-400 border border-red-500/30">${player.injury_status || 'INJ'}</span>`
            : '';
        const valueBadge = renderValueBadge(player.id);
        const notesBadge = player.custom_notes
            ? `<span class="ml-2 px-1.5 py-0.5 text-xs rounded-full bg-yellow-500/20 text-yellow-400 border border-yellow-500/30" title="${escapeHtml(player.custom_notes)}">NOTE</span>`
            : '';

        return `
        <tr class="player-row border-b border-gray-800/50 ${player.is_drafted ? 'drafted' : ''} ${highlightClass} ${index < 30 ? 'animate-fade-in' : ''} cursor-pointer" data-player-id="${player.id}" onclick="showPlayerDetail(${player.id})" style="animation-delay: ${index < 30 ? index * 8 : 0}ms">
            <td class="py-3 px-3">
                <span class="font-mono text-lg font-bold text-gray-300">${player.consensus_rank || '--'}</span>
            </td>
            <td class="py-3 px-2">
                <span class="font-semibold hover:text-emerald-400 transition-colors">${player.name}</span>
                ${injuryBadge}
                ${valueBadge}
                ${notesBadge}
            </td>
            <td class="py-3 px-2">
                <span class="text-gray-400 font-medium">${player.team || '--'}</span>
            </td>
            <td class="py-3 px-2">
                ${renderPositionBadge(player.positions)}
            </td>
            <td class="py-3 px-2">
                ${(() => { const t = getPlayerTierForDisplay(player); return t ? renderTierBadge(t.tier_name, t.tier_order) : '<span class="text-gray-600">--</span>'; })()}
            </td>
            <td class="py-3 px-2">
                ${renderOverallRank(player.last_season_rank)}
            </td>
            <td class="py-3 px-2">
                ${renderPosRank(player.last_season_pos_rank)}
            </td>
            <td class="py-3 px-2">
                ${renderRiskScore(player.risk_score)}
            </td>
            <td class="py-3 px-2">
                ${renderSurplusValue(player.id)}
            </td>
            <td class="py-3 px-2" onclick="event.stopPropagation()">
                <div class="flex items-center gap-1">
                ${player.is_drafted
                    ? '<span class="action-btn drafted-label">Drafted</span>'
                    : (() => {
                        const isMyTurn = draftSessionId && draftIsUserPick;
                        const btnLabel = draftSessionId ? (isMyTurn ? 'DRAFT' : 'ADD') : 'Draft';
                        const btnClass = isMyTurn ? 'action-btn draft my-pick' : 'action-btn draft';
                        return `<button onclick="draftPlayer(${player.id})" class="${btnClass}">${btnLabel}</button>
                    <button onclick="toggleCompare(${player.id}, event)" class="action-btn compare${compareSlots.includes(player.id) ? ' active' : ''}" title="Compare">
                        ${compareSlots.includes(player.id) ? 'CMP' : 'vs'}
                    </button>`;
                    })()
                }
                </div>
            </td>
        </tr>
    `}).join('');
}

// Render '25 Overall rank with colored text
function renderOverallRank(rank) {
    if (!rank) return '<span class="text-gray-600">--</span>';
    let color;
    if (rank <= 50) color = 'text-green-400';
    else if (rank <= 150) color = 'text-emerald-300';
    else if (rank <= 250) color = 'text-gray-400';
    else if (rank <= 400) color = 'text-orange-400';
    else color = 'text-red-400';
    return `<span class="${color}">${rank}</span>`;
}

// Render '25 Pos rank with colored text
function renderPosRank(rank) {
    if (!rank) return '<span class="text-gray-600">--</span>';
    let color;
    if (rank <= 5) color = 'text-green-400';
    else if (rank <= 15) color = 'text-emerald-300';
    else if (rank <= 25) color = 'text-gray-400';
    else if (rank <= 40) color = 'text-orange-400';
    else color = 'text-red-400';
    return `<span class="${color}">${rank}</span>`;
}

// Render risk score with colored text
function renderRiskScore(score) {
    if (score === null || score === undefined) return '<span class="text-gray-600">--</span>';
    const rounded = Math.round(score);
    let color;
    if (score < 30) color = 'text-green-400';
    else if (score < 60) color = 'text-yellow-400';
    else color = 'text-red-400';
    return `<span class="${color}" title="Risk score: ${rounded}">${rounded}</span>`;
}

// Render sleeper/bust value badge
function renderValueBadge(playerId) {
    const classification = valueClassifications[playerId];
    if (!classification) return '';

    if (classification.classification === 'sleeper') {
        const diff = Math.round(classification.difference);
        return `<span class="value-badge sleeper" title="${classification.description}">Sleeper +${diff}</span>`;
    } else if (classification.classification === 'bust_risk') {
        const diff = Math.round(Math.abs(classification.difference));
        return `<span class="value-badge bust" title="${classification.description}">Bust Risk -${diff}</span>`;
    }

    return '';
}

// Render position with color-coded badge
function renderPositionBadge(positions) {
    if (!positions) return '<span class="text-gray-500">--</span>';

    const pos = positions.split(/[\/,]/)[0].trim().toUpperCase();

    let badgeClass = 'infield';
    if (['SP', 'RP', 'P'].includes(pos)) badgeClass = 'pitcher';
    else if (pos === 'C') badgeClass = 'catcher';
    else if (['OF', 'LF', 'CF', 'RF', 'DH'].includes(pos)) badgeClass = 'outfield';

    return `<span class="position-badge ${badgeClass}">${positions}</span>`;
}

// Tier badge rendering
const TIER_CLASS_MAP = {
    1: 'legendary',
    2: 'epic',
    3: 'rare',
    4: 'uncommon-tier',
    5: 'common-tier',
    6: 'low-tier',
    7: 'leftover',
    8: 'next-in-line',
};

function renderTierBadge(tierName, tierOrder) {
    const cls = TIER_CLASS_MAP[tierOrder] || 'leftover';
    return `<span class="tier-badge ${cls}">${tierOrder}</span>`;
}

function getPlayerTierForDisplay(player) {
    const tiers = player.position_tiers;
    if (!tiers || tiers.length === 0) return null;

    // If a single position is selected, prefer that position's tier
    if (selectedPositions.size === 1) {
        const filterPos = [...selectedPositions][0];
        const match = tiers.find(t => t.position === filterPos);
        if (match) return match;
    }

    // Fall back to primary_position tier
    if (player.primary_position) {
        const primary = tiers.find(t => t.position === player.primary_position);
        if (primary) return primary;
    }

    // Fall back to best (lowest order) tier
    return tiers.reduce((best, t) => (!best || t.tier_order < best.tier_order) ? t : best, null);
}

// Render a risk component bar for the modal
function renderRiskComponent(label, component) {
    if (!component) return '';

    const score = component.score || 0;
    const detail = component.detail || '';

    // Color based on score (lower = better/safer)
    let barColor = 'bg-emerald-500';
    let textColor = 'text-emerald-400';
    if (score >= 60) {
        barColor = 'bg-red-500';
        textColor = 'text-red-400';
    } else if (score >= 30) {
        barColor = 'bg-yellow-500';
        textColor = 'text-yellow-400';
    }

    return `
        <div class="text-xs">
            <div class="flex justify-between mb-1">
                <span class="text-gray-300">${label}</span>
                <span class="${textColor}">${Math.round(score)}</span>
            </div>
            <div class="h-1.5 bg-gray-600 rounded overflow-hidden">
                <div class="${barColor} h-full rounded" style="width: ${score}%"></div>
            </div>
            <div class="text-gray-500 mt-0.5">${detail}</div>
        </div>
    `;
}

// Load recommendations
async function loadRecommendations() {
    if (!currentLeagueId) return;

    // Show loading states
    showSectionLoading('hero-pick', 'Analyzing top picks...');
    showSectionLoading('also-consider-picks', 'Loading additional picks...');
    showSectionLoading('prospect-picks', 'Evaluating prospects...');
    showSectionLoading('planner-dashboard', 'Building category plan...');

    try {
        recommendations = await fetchAPI(withUserKey(`/recommendations/${currentLeagueId}`));
        try {
            await loadCategoryPlanner();
        } catch (plannerError) {
            console.warn('Category planner unavailable:', plannerError?.message || plannerError);
            categoryPlanner = null;
            renderCategoryPlannerUnavailable(plannerError?.message || 'Planner unavailable');
        }
        renderRecommendations();
        updateDraftInfo(recommendations);
    } catch (error) {
        console.error('Failed to load recommendations:', error);
        showError(`Failed to load recommendations: ${error.message}`, 'error');
        showSectionError('hero-pick', error.message);
        showSectionError('also-consider-picks', error.message);
        showSectionError('prospect-picks', error.message);
        showSectionError('planner-dashboard', error.message);
    }
}

async function loadCategoryPlanner() {
    if (!currentLeagueId) return;
    categoryPlanner = await fetchAPI(withUserKey(`/recommendations/${currentLeagueId}/planner`));
    renderCategoryPlanner();
}

function renderCategoryPlannerUnavailable(message = 'Planner unavailable') {
    const container = document.getElementById('planner-dashboard');
    if (!container) return;

    const isSetupIssue = String(message).toLowerCase().includes('user team not set');
    const body = isSetupIssue
        ? 'Claim your team in the Teams tab to enable planner tracking.'
        : 'Planner data is temporarily unavailable. Recommendations are still loaded.';

    container.innerHTML = `
        <div class="bg-gray-900/60 border border-gray-700 rounded-lg p-3">
            <p class="text-sm text-indigo-300 mb-1">Category Planner</p>
            <p class="text-xs text-gray-400">${escapeHtml(body)}</p>
        </div>
    `;
}

function renderCategoryPlanner() {
    const container = document.getElementById('planner-dashboard');
    if (!container || !categoryPlanner) return;

    const focusCards = (categoryPlanner.focus_plan || []).map(focus => `
        <div class="bg-gray-800/60 border border-gray-700 rounded-lg p-3">
            <div class="flex justify-between items-center mb-2">
                <span class="text-sm font-semibold text-amber-300">${focus.category.toUpperCase()}</span>
                <span class="text-xs text-red-300">${Number(focus.deficit_pct).toFixed(1)}% deficit</span>
            </div>
            <p class="text-xs text-gray-400 mb-2">Priority positions: ${escapeHtml(focus.suggested_positions)}</p>
            ${(focus.top_options || []).length > 0 ? `
                <div class="space-y-1">
                    ${(focus.top_options || []).map(o => `
                        <div class="flex justify-between text-xs">
                            <button onclick="showPlayerDetail(${o.player_id})" class="text-blue-300 hover:text-blue-200">
                                ${escapeHtml(o.player_name)} (${escapeHtml(o.positions)})
                            </button>
                            <span class="text-gray-400">+${o.estimated_gain}</span>
                        </div>
                    `).join('')}
                </div>
            ` : '<p class="text-xs text-gray-500">No clear options in current player pool.</p>'}
        </div>
    `).join('');

    const topNeedsRows = (categoryPlanner.needs || []).slice(0, 6).map(need => `
        <tr class="border-b border-gray-800">
            <td class="py-1 text-xs font-medium ${need.status === 'behind' ? 'text-red-300' : 'text-emerald-300'}">${need.category.toUpperCase()}</td>
            <td class="py-1 text-xs text-right text-gray-300">${need.projected_final}</td>
            <td class="py-1 text-xs text-right text-gray-400">${need.target}</td>
            <td class="py-1 text-xs text-right ${need.gap > 0 ? 'text-red-300' : 'text-emerald-300'}">${need.gap > 0 ? '+' : ''}${need.gap}</td>
        </tr>
    `).join('');

    const targetInputs = Object.entries(categoryPlanner.targets || {}).map(([category, value]) => {
        const isRatio = ['avg', 'ops', 'era', 'whip'].includes(category);
        return `
            <label class="flex items-center justify-between gap-2 text-xs">
                <span class="text-gray-400 w-10">${category.toUpperCase()}</span>
                <input
                    data-category="${category}"
                    class="planner-target-input bg-gray-900 border border-gray-700 rounded px-2 py-1 w-24 text-right text-gray-200"
                    type="number"
                    step="${isRatio ? '0.001' : '1'}"
                    value="${value}"
                />
            </label>
        `;
    }).join('');

    container.innerHTML = `
        <div class="bg-gray-900/60 border border-indigo-700/40 rounded-lg p-3 mb-3">
            <div class="flex items-center justify-between mb-2">
                <h4 class="text-sm font-semibold text-indigo-300">Draft Pace Planner</h4>
                <span class="text-xs text-gray-400">${categoryPlanner.team_picks_made}/${categoryPlanner.team_pick_target} picks</span>
            </div>
            <p class="text-xs text-gray-300 mb-2">${escapeHtml(categoryPlanner.summary)}</p>
            <div class="h-2 rounded bg-gray-800 overflow-hidden mb-1">
                <div class="h-full bg-indigo-500" style="width: ${Math.max(2, categoryPlanner.completion_pct)}%"></div>
            </div>
            <p class="text-[11px] text-gray-500">${categoryPlanner.completion_pct}% draft completion</p>
        </div>

        <div class="grid grid-cols-1 gap-3 mb-3">
            ${focusCards || '<p class="text-xs text-gray-500">No major deficits detected.</p>'}
        </div>

        <div class="bg-gray-900/60 border border-gray-700 rounded-lg p-3 mb-3">
            <p class="text-xs uppercase tracking-wide text-gray-500 mb-2">Top Category Gaps</p>
            <table class="w-full">
                <thead>
                    <tr class="text-[11px] text-gray-500">
                        <th class="text-left py-1">Cat</th>
                        <th class="text-right py-1">Proj</th>
                        <th class="text-right py-1">Target</th>
                        <th class="text-right py-1">Gap</th>
                    </tr>
                </thead>
                <tbody>${topNeedsRows}</tbody>
            </table>
        </div>

        <div class="bg-gray-900/60 border border-gray-700 rounded-lg p-3">
            <div class="flex items-center justify-between mb-2">
                <p class="text-xs uppercase tracking-wide text-gray-500">Custom Targets</p>
                <div class="flex gap-2">
                    <button onclick="savePlannerTargets()" class="text-xs px-2 py-1 bg-indigo-600 hover:bg-indigo-500 rounded">Save</button>
                    <button onclick="resetPlannerTargets()" class="text-xs px-2 py-1 border border-gray-600 hover:bg-gray-800 rounded">Reset</button>
                </div>
            </div>
            <div class="grid grid-cols-2 sm:grid-cols-3 gap-2">${targetInputs}</div>
        </div>
    `;
}

async function savePlannerTargets() {
    const inputs = document.querySelectorAll('.planner-target-input');
    const targets = {};
    inputs.forEach(input => {
        const category = input.dataset.category;
        const value = Number(input.value);
        if (category && Number.isFinite(value) && value > 0) {
            targets[category] = value;
        }
    });
    categoryPlanner = await fetchAPI(withUserKey(`/recommendations/${currentLeagueId}/planner`), {
        method: 'POST',
        body: JSON.stringify({ targets }),
    });
    renderCategoryPlanner();
}

async function resetPlannerTargets() {
    categoryPlanner = await fetchAPI(withUserKey(`/recommendations/${currentLeagueId}/planner`), {
        method: 'POST',
        body: JSON.stringify({ targets: {} }),
    });
    renderCategoryPlanner();
}

// Load scarcity data
async function loadScarcity() {
    if (!currentLeagueId) return;
    try {
        scarcityData = await fetchAPI(`/recommendations/${currentLeagueId}/scarcity`);
        renderScarcityDashboard();
    } catch (error) {
        console.error('Failed to load scarcity data:', error);
        showError(`Failed to load scarcity data: ${error.message}`, 'error');
    }
}

// Render scarcity dashboard widget
function renderScarcityDashboard() {
    const container = document.getElementById('scarcity-dashboard');
    if (!container || !scarcityData) return;

    const FIXED_ORDER = ['C', '1B', '2B', '3B', 'SS', 'OF', 'SP', 'RP'];
    const BADGE_LABELS = { critical: 'CRIT', high: 'HIGH', moderate: 'MOD', low: 'LOW' };

    // Alerts section — prefix with position badge extracted from alert text
    let alertsHtml = '';
    if (scarcityData.alerts && scarcityData.alerts.length > 0) {
        alertsHtml = `<div class="mb-3 space-y-2">
            ${scarcityData.alerts.map(alert => {
                const posMatch = alert.match(/^(\w+)\s/);
                const posTag = posMatch ? `<span class="scarcity-urgency-badge critical" style="font-size:0.45rem;padding:0.05rem 0.25rem;width:auto;">${escapeHtml(posMatch[1])}</span> ` : '';
                return `<div class="scarcity-alert-item p-2 rounded text-xs flex items-center gap-2">
                    ${posTag}<span>${escapeHtml(alert)}</span>
                </div>`;
            }).join('')}
        </div>`;
    }

    // Position rows in fixed order
    const rowsHtml = FIXED_ORDER.map(pos => {
        const data = scarcityData.positions[pos];
        if (!data) return '';

        const urgency = data.urgency || 'low';
        const tc = data.tier_counts || {};
        const elite = tc.elite || 0;
        const eliteTotal = tc.elite_total || 0;
        const multiplier = data.scarcity_multiplier.toFixed(2);

        // Build HP bar segments
        const denseClass = eliteTotal > 12 ? ' dense' : '';
        const shineDelay = FIXED_ORDER.indexOf(pos) * 0.5;

        let segsHtml = '';
        for (let i = 0; i < eliteTotal; i++) {
            segsHtml += i < elite
                ? `<div class="hp-seg filled ${urgency}"></div>`
                : `<div class="hp-seg empty"></div>`;
        }

        const barHtml = `<div class="hp-bar-wrapper">
            <div class="hp-bar-track ${urgency}">
                <div class="hp-bar-segments${denseClass}">${segsHtml}</div>
                <div class="hp-bar-shine" style="--shine-delay:${shineDelay}s"></div>
            </div>
            <span class="hp-bar-fraction ${urgency}">${elite}/${eliteTotal}</span>
        </div>`;

        // Urgency badge — pulse if tier_dropoff
        const badgeLabel = BADGE_LABELS[urgency] || 'LOW';
        const pulseClass = data.tier_dropoff ? ' pulse' : '';

        return `<div class="flex items-center gap-2 py-1 px-2 rounded hover:bg-gray-800/50">
            <span class="scarcity-pos-label" style="color: var(--neon-${urgency === 'critical' ? 'red' : urgency === 'high' ? 'amber' : urgency === 'moderate' ? 'yellow' : 'green'})">${escapeHtml(pos)}</span>
            <span class="scarcity-urgency-badge ${urgency}${pulseClass}">${badgeLabel}</span>
            ${barHtml}
            <span class="scarcity-tier-counts"><span class="tc-val">${tc.top_25 || 0}</span><span class="tc-sep">|</span><span class="tc-val">${tc.top_100 || 0}</span><span class="tc-sep">|</span><span class="tc-val">${tc.total || 0}</span></span>
            <span class="scarcity-multiplier" style="color: var(--neon-${urgency === 'critical' ? 'red' : urgency === 'high' ? 'amber' : urgency === 'moderate' ? 'yellow' : 'green'})">${multiplier}x</span>
        </div>`;
    }).join('');

    // Column header for tier counts
    const headerHtml = `<div class="flex items-center gap-2 py-0.5 px-2">
        <span class="scarcity-pos-label"></span>
        <span class="scarcity-urgency-badge" style="visibility:hidden">LOW</span>
        <div class="hp-bar-wrapper"><span class="hp-bar-header-label">Elite HP</span></div>
        <span class="scarcity-tier-counts scarcity-header"><span class="tc-val">T25</span><span class="tc-sep">|</span><span class="tc-val">T100</span><span class="tc-sep">|</span><span class="tc-val">Avl</span></span>
        <span class="scarcity-multiplier"></span>
    </div>`;

    // Legend row
    const legendHtml = `<div class="scarcity-legend">
        <span class="scarcity-legend-item"><span class="hp-seg-legend filled"></span> elite avail.</span>
        <span class="scarcity-legend-item"><span class="hp-seg-legend empty"></span> elite drafted</span>
    </div>`;

    container.innerHTML = alertsHtml + `<div class="space-y-0.5">${headerHtml}${rowsHtml}</div>` + legendHtml;
}

// Render recommendations
function renderRecommendations() {
    // Build player category map for highlighting in the draft board
    playerCategories = {};

    // Mark recommended players (highest priority)
    (recommendations.recommended || []).forEach(pick => {
        playerCategories[pick.player.id] = 'recommended';
    });

    // Mark safe players (if not already recommended)
    (recommendations.safe || []).forEach(pick => {
        if (!playerCategories[pick.player.id]) {
            playerCategories[pick.player.id] = 'safe';
        }
    });

    // Mark risky players (if not already categorized)
    (recommendations.risky || []).forEach(pick => {
        if (!playerCategories[pick.player.id]) {
            playerCategories[pick.player.id] = 'risky';
        }
    });

    // Mark needs-based players (if not already categorized)
    (recommendations.category_needs || []).forEach(pick => {
        if (!playerCategories[pick.player.id]) {
            playerCategories[pick.player.id] = 'needs';
        }
    });

    // Mark prospects (pink highlight - can coexist with other categories)
    (recommendations.prospects || []).forEach(pick => {
        // Prospects get their own highlight if not already categorized
        if (!playerCategories[pick.player.id]) {
            playerCategories[pick.player.id] = 'prospect';
        }
    });

    // Re-render player list with highlights
    renderPlayerList();

    // ── DECISION FUNNEL RENDERING ────────────────────────────────────────

    // 1. Hero Pick — the single top recommendation
    const heroPick = (recommendations.recommended || [])[0];
    const heroContainer = document.getElementById('hero-pick');
    if (heroPick) {
        heroContainer.innerHTML = `
            <div class="recommendation-card recommended hero-pick-card animate-slide-up">
                <div class="flex justify-between items-start mb-3">
                    <div class="flex items-center gap-3">
                        <span class="hero-pick-number">1</span>
                        <div>
                            <button onclick="showPlayerDetail(${heroPick.player.id})" class="hero-pick-name hover:text-yellow-400 transition-colors">
                                ${heroPick.player.name}
                            </button>
                            <div class="flex items-center gap-2 mt-1">
                                <span class="text-sm text-gray-400">${heroPick.player.team || 'FA'}</span>
                                ${renderPositionBadge(heroPick.player.positions)}
                            </div>
                        </div>
                    </div>
                    <div class="flex flex-col items-end gap-1.5">
                        <span class="risk-badge ${
                            heroPick.risk_level === 'low' ? 'safe' :
                            heroPick.risk_level === 'medium' ? 'moderate' : 'risky'
                        }">${heroPick.risk_level}</span>
                        <span class="text-sm font-mono text-gray-400">#${heroPick.player.consensus_rank || '--'}</span>
                    </div>
                </div>
                <p class="text-sm text-white font-medium mb-3 leading-relaxed">${heroPick.summary}</p>
                <ul class="text-sm text-gray-300 mb-3 space-y-1.5">
                    ${(heroPick.reasoning || []).map(r => `
                        <li class="flex items-start gap-2">
                            <span class="text-yellow-400 mt-0.5 flex-shrink-0">→</span>
                            <span>${r}</span>
                        </li>
                    `).join('')}
                </ul>
                ${(heroPick.sources || []).length > 0 ? `
                    <div class="flex flex-wrap gap-2 pt-3 border-t border-gray-700/50">
                        ${(heroPick.sources || []).slice(0, 4).map(s => `
                            <a href="${s.url || '#'}" target="_blank" class="source-chip">
                                ${s.name} <span class="font-mono">#${s.rank || '--'}</span>
                            </a>
                        `).join('')}
                    </div>
                ` : ''}
            </div>`;
    } else {
        heroContainer.innerHTML = '<p class="text-gray-500 text-sm py-2">Loading recommendations...</p>';
    }

    // 2. Also Consider — unified compact list merging all remaining picks
    const alsoConsiderItems = [];

    // Remaining recommended picks (index 1+)
    (recommendations.recommended || []).slice(1).forEach(p =>
        alsoConsiderItems.push({ pick: p, pickType: 'recommended', typeLabel: 'TOP PICK',
            hoverColor: 'hover:text-yellow-400', detail: p.summary }));

    // Safe picks
    (recommendations.safe || []).forEach(p =>
        alsoConsiderItems.push({ pick: p, pickType: 'safe', typeLabel: 'SAFE',
            hoverColor: 'hover:text-emerald-400', detail: p.rationale }));

    // Risky picks
    (recommendations.risky || []).forEach(p =>
        alsoConsiderItems.push({ pick: p, pickType: 'risky', typeLabel: 'RISKY',
            hoverColor: 'hover:text-orange-400', detail: p.upside ? `Upside: ${p.upside}` : p.rationale }));

    // Needs-based picks
    (recommendations.category_needs || []).forEach(p =>
        alsoConsiderItems.push({ pick: p, pickType: 'needs', typeLabel: `FILLS ${(p.need_addressed || 'GAP').toUpperCase()}`,
            hoverColor: 'hover:text-blue-400',
            detail: `+${Math.round((p.projected_strength || 0) - (p.current_strength || 0))}% ${p.need_addressed || ''}` }));

    const alsoConsiderContainer = document.getElementById('also-consider-picks');
    if (alsoConsiderItems.length > 0) {
        alsoConsiderContainer.innerHTML = alsoConsiderItems.map((item, index) => `
            <div class="compact-pick-row animate-slide-up" style="animation-delay: ${index * 40}ms" title="${item.detail}">
                <button onclick="showPlayerDetail(${item.pick.player.id})" class="compact-pick-name ${item.hoverColor}">
                    ${item.pick.player.name}
                </button>
                <div class="compact-pick-meta">
                    <span class="text-xs text-gray-500">${item.pick.player.team || 'FA'}</span>
                    ${renderPositionBadge(item.pick.player.positions)}
                </div>
                <div class="compact-pick-right">
                    <span class="pick-type-badge pick-type-${item.pickType}">${item.typeLabel}</span>
                    <span class="text-xs text-gray-500 font-mono">#${item.pick.player.consensus_rank || '--'}</span>
                </div>
            </div>
        `).join('');
    } else {
        alsoConsiderContainer.innerHTML = '<p class="text-gray-500 text-sm py-2">No additional picks available</p>';
    }

    // 3. Prospects (keeper-focused collapsible)
    const prospectsContainer = document.getElementById('prospect-picks');
    prospectsContainer.innerHTML = (recommendations.prospects || []).map((pick, index) => `
        <div class="compact-pick-row animate-slide-up" style="animation-delay: ${index * 50}ms">
            <div class="flex-1 min-w-0">
                <button onclick="showPlayerDetail(${pick.player.id})" class="compact-pick-name hover:text-purple-400 block">
                    ${pick.player.name}
                </button>
                <div class="flex items-center gap-1.5 mt-0.5">
                    <span class="text-xs text-gray-500">${pick.player.team || 'MiLB'}</span>
                    ${renderPositionBadge(pick.player.positions)}
                    <span class="eta-badge">ETA ${pick.eta}</span>
                </div>
            </div>
            <div class="flex flex-col items-end gap-1 flex-shrink-0">
                ${pick.prospect_rank ? `<span class="prospect-rank-badge">#${pick.prospect_rank}</span>` : ''}
                <span class="keeper-value-badge ${pick.keeper_value}">${pick.keeper_value}</span>
            </div>
        </div>
    `).join('') || '<p class="text-gray-500 text-sm py-2">No prospects available</p>';
}

// Update draft info
function updateDraftInfo(rec) {
    document.getElementById('current-pick-info').textContent =
        `Pick ${rec.current_pick} - Round ${Math.ceil(rec.current_pick / 12)}`;

    if (rec.picks_until_your_turn !== null) {
        const clockDiv = document.getElementById('on-the-clock');
        clockDiv.classList.remove('hidden');
        document.getElementById('clock-team').textContent =
            rec.picks_until_your_turn === 0 ? "YOU'RE UP!" : `${rec.picks_until_your_turn} picks away`;
    }

    // Update the sidebar pick status banner
    const banner = document.getElementById('sidebar-pick-banner');
    const bannerText = document.getElementById('sidebar-banner-text');
    if (banner && bannerText) {
        if (rec.picks_until_your_turn === 0) {
            banner.className = 'pick-status-banner pick-your-turn mb-3';
            bannerText.textContent = `YOUR PICK — Round ${Math.ceil(rec.current_pick / 12)}, Pick ${rec.current_pick}`;
        } else if (rec.picks_until_your_turn !== null) {
            banner.className = 'pick-status-banner pick-waiting mb-3';
            bannerText.textContent = `${rec.picks_until_your_turn} pick${rec.picks_until_your_turn === 1 ? '' : 's'} until your turn`;
        } else {
            banner.className = 'pick-status-banner pick-waiting mb-3';
            bannerText.textContent = `Round ${Math.ceil(rec.current_pick / 12)} · Pick ${rec.current_pick}`;
        }
    }
}

// Current player in modal (for actions)
let currentModalPlayer = null;

// Show player detail modal
async function showPlayerDetail(playerId) {
    console.log('[DEBUG] showPlayerDetail called with playerId:', playerId);

    // Show modal with loading state using our new utility function
    showModalLoading();

    try {
        // Fetch player data and risk assessment in parallel
        const [player, riskAssessment] = await Promise.all([
            fetchAPI(`/players/${playerId}`),
            fetchAPI(`/players/${playerId}/risk-assessment`).catch(() => null)
        ]);

        currentModalPlayer = player;

        // Hide loading using our new utility function
        hideModalLoading();

        // Position badge with color coding
        const positionColors = {
            'C': 'bg-amber-700', 'SS': 'bg-blue-700', '2B': 'bg-blue-600',
            '3B': 'bg-indigo-700', '1B': 'bg-purple-700', 'OF': 'bg-emerald-700',
            'SP': 'bg-red-700', 'RP': 'bg-orange-700', 'DH': 'bg-gray-600'
        };
        const pos = player.primary_position || 'N/A';
        const posColor = positionColors[pos] || 'bg-gray-700';
        document.getElementById('modal-position-badge').className = `w-12 h-12 rounded-lg flex items-center justify-center text-lg font-bold ${posColor}`;
        document.getElementById('modal-position-badge').textContent = pos;

        // Header info
        document.getElementById('modal-player-name').textContent = player.name;
        const teamChangeHtml = player.previous_team && player.previous_team !== player.team
            ? ` <span class="new-team-badge" title="Was ${escapeHtml(player.previous_team)}">NEW</span>`
            : '';
        document.getElementById('modal-player-info').innerHTML =
            `${escapeHtml(player.team || 'FA')}${teamChangeHtml} | ${escapeHtml(player.positions || 'N/A')}`;

        // Injury banner
        const injuryBanner = document.getElementById('modal-injury-banner');
        if (player.is_injured) {
            injuryBanner.classList.remove('hidden');
            document.getElementById('modal-injury-text').textContent =
                player.injury_details || player.injury_status || 'Currently injured';
        } else {
            injuryBanner.classList.add('hidden');
        }

        // Quick Stats
        console.log('Player data:', player.name, 'age:', player.age, 'rank:', player.consensus_rank);
        document.getElementById('modal-age').textContent = player.age != null ? player.age : '--';

        // ADP Gap (ESPN ADP vs Average Ranking)
        const adpGapEl = document.getElementById('modal-adp-gap');
        const rankings = player.rankings || [];

        // Find ESPN ADP
        const espnRanking = rankings.find(r => r.source_name && r.source_name.toLowerCase().includes('espn') && r.adp);
        const espnAdp = espnRanking ? espnRanking.adp : null;

        // Calculate average rank from all sources with overall_rank
        const ranksWithOverall = rankings.filter(r => r.overall_rank != null);
        const avgRank = ranksWithOverall.length > 0
            ? ranksWithOverall.reduce((sum, r) => sum + r.overall_rank, 0) / ranksWithOverall.length
            : null;

        if (espnAdp != null && avgRank != null) {
            // Positive = being drafted later than ranked (value), Negative = drafted earlier (reach)
            const gap = Math.round(espnAdp - avgRank);
            adpGapEl.textContent = gap > 0 ? `+${gap}` : `${gap}`;
            // Green for value picks, red for reaches
            adpGapEl.className = `text-xl font-bold ${gap > 5 ? 'text-emerald-400' : gap < -5 ? 'text-red-400' : 'text-white'}`;
        } else if (avgRank != null) {
            adpGapEl.textContent = '--';
            adpGapEl.className = 'text-xl font-bold text-gray-500';
        } else {
            adpGapEl.textContent = '--';
            adpGapEl.className = 'text-xl font-bold text-gray-500';
        }

        // Rank
        document.getElementById('modal-rank').textContent = player.consensus_rank != null ? `#${player.consensus_rank}` : '--';

        // Avg ADP (ESPN + FantasyPros average)
        const avgAdpEl = document.getElementById('modal-avg-adp');
        const espnAdpRanking = rankings.find(r => r.source_name && r.source_name.toLowerCase().includes('espn') && r.adp);
        const fpAdpRanking = rankings.find(r => r.source_name && r.source_name.toLowerCase().includes('fantasypros') && r.adp);

        const adpValues = [
            espnAdpRanking?.adp,
            fpAdpRanking?.adp
        ].filter(v => v != null);

        if (adpValues.length > 0) {
            const avgAdp = adpValues.reduce((a, b) => a + b, 0) / adpValues.length;
            avgAdpEl.textContent = Math.round(avgAdp);
        } else {
            avgAdpEl.textContent = '--';
        }

        // Compact Position Tier in quick stats row
        const tierEl = document.getElementById('modal-position-tier');
        if (player.position_tiers && player.position_tiers.length > 0) {
            const sorted = [...player.position_tiers].sort((a, b) => a.tier_order - b.tier_order);
            const primary = sorted[0];
            tierEl.textContent = `${primary.position} · ${primary.tier_name}`;
            const colors = ['text-emerald-400', 'text-yellow-400', 'text-orange-400', 'text-red-400'];
            tierEl.className = `text-sm font-bold ${colors[Math.min(primary.tier_order - 1, 3)]}`;
        } else {
            tierEl.textContent = '--';
            tierEl.className = 'text-sm font-bold text-gray-500';
        }

        // Custom Notes
        const notesSection = document.getElementById('modal-notes-section');
        const notesDisplay = document.getElementById('modal-notes-display');
        const notesEdit = document.getElementById('modal-notes-edit');
        notesSection.classList.remove('hidden');
        notesEdit.classList.add('hidden');
        const existingBanner = document.getElementById('notes-restore-banner');
        if (existingBanner) existingBanner.remove();
        if (player.custom_notes) {
            notesDisplay.textContent = player.custom_notes;
            notesDisplay.classList.remove('hidden');
            document.getElementById('modal-notes-edit-btn').textContent = 'Edit';
        } else {
            notesDisplay.classList.add('hidden');
            document.getElementById('modal-notes-edit-btn').textContent = 'Add Notes';
            const localNote = localNotesGet(player.id);
            if (localNote) {
                const banner = document.createElement('div');
                banner.id = 'notes-restore-banner';
                banner.className = 'mt-2 p-2 bg-yellow-900/50 border border-yellow-600 rounded text-xs text-yellow-300 flex items-center justify-between gap-2';
                banner.innerHTML = '<span>&#x26A0; A local backup of your note was found.</span><button class="underline font-medium hover:text-yellow-100" onclick="restoreLocalNote()">Restore</button>';
                notesSection.appendChild(banner);
            }
        }

        // Scarcity Context
        const scarcitySection = document.getElementById('modal-scarcity');
        const scarcityContent = document.getElementById('modal-scarcity-content');
        if (player.scarcity_context && !player.is_drafted) {
            const sc = player.scarcity_context;
            scarcitySection.classList.remove('hidden');
            let tierAlertHtml = '';
            if (sc.tier_alert) {
                tierAlertHtml = `<div class="mt-2 p-2 bg-red-900/50 border border-red-700 rounded text-xs text-red-300 font-medium">${escapeHtml(sc.tier_alert)}</div>`;
            }
            scarcityContent.innerHTML = `
                <div class="grid grid-cols-3 gap-3 text-center">
                    <div class="bg-gray-800/60 rounded-lg p-2">
                        <div class="text-xs text-gray-400">Multiplier</div>
                        <div class="text-lg font-bold text-red-400">${sc.scarcity_multiplier.toFixed(2)}x</div>
                    </div>
                    <div class="bg-gray-800/60 rounded-lg p-2">
                        <div class="text-xs text-gray-400">Tier 1</div>
                        <div class="text-lg font-bold ${sc.tier1_remaining <= 2 ? 'text-red-400' : sc.tier1_remaining <= Math.floor(sc.tier1_total / 2) ? 'text-orange-400' : 'text-green-400'}">${sc.tier1_remaining} / ${sc.tier1_total}</div>
                    </div>
                    <div class="bg-gray-800/60 rounded-lg p-2">
                        <div class="text-xs text-gray-400">Adj. Rank</div>
                        <div class="scarcity-adjusted-rank text-lg font-bold">${sc.adjusted_rank != null ? '#' + sc.adjusted_rank : '--'}</div>
                    </div>
                </div>
                <p class="text-xs text-gray-400 mt-2">${escapeHtml(sc.supply_message)}</p>
                ${tierAlertHtml}
            `;
        } else {
            scarcitySection.classList.add('hidden');
        }

        // Prospect Info
        const prospectInfo = document.getElementById('modal-prospect-info');
        if (player.is_prospect && player.prospect_profile) {
            prospectInfo.classList.remove('hidden');
            const pp = player.prospect_profile;
            document.getElementById('modal-fv-grade').textContent = pp.future_value || '--';
            document.getElementById('modal-eta').textContent = pp.eta || '--';
            document.getElementById('modal-level').textContent = pp.current_level || '--';
        } else if (player.is_prospect) {
            prospectInfo.classList.remove('hidden');
            document.getElementById('modal-fv-grade').textContent = '--';
            document.getElementById('modal-eta').textContent = '--';
            document.getElementById('modal-level').textContent = '--';
        } else {
            prospectInfo.classList.add('hidden');
        }

        // Risk Assessment
        const riskContent = document.getElementById('modal-risk-content');
        if (riskAssessment) {
            const score = riskAssessment.overall_score;
            const classification = riskAssessment.classification;
            const classColor = classification === 'safe' ? 'text-emerald-400' :
                              classification === 'moderate' ? 'text-yellow-400' : 'text-red-400';
            const classBg = classification === 'safe' ? 'bg-emerald-900/30' :
                           classification === 'moderate' ? 'bg-yellow-900/30' : 'bg-red-900/30';

            riskContent.innerHTML = `
                <div class="flex justify-between items-center mb-3">
                    <span class="text-sm font-medium">Overall Risk</span>
                    <div class="flex items-center gap-2">
                        <span class="${classColor} font-bold text-lg">${Math.round(score)}/100</span>
                        <span class="text-xs px-2 py-1 rounded ${classBg} ${classColor}">${classification.toUpperCase()}</span>
                    </div>
                </div>
                ${riskAssessment.factors && riskAssessment.factors.length > 0 ? `
                    <div class="mb-3">
                        <div class="flex flex-wrap gap-1">
                            ${riskAssessment.factors.map(f => `
                                <span class="text-xs px-2 py-1 rounded bg-red-900/40 text-red-300">${f}</span>
                            `).join('')}
                        </div>
                    </div>
                ` : ''}
                ${riskAssessment.upside ? `
                    <div class="mb-3 p-2 rounded bg-emerald-900/20 border border-emerald-800/50">
                        <span class="text-xs text-emerald-400 font-medium">Upside:</span>
                        <span class="text-xs text-emerald-300 ml-1">${riskAssessment.upside}</span>
                    </div>
                ` : ''}
                <div class="grid grid-cols-2 gap-2 mt-3">
                    ${renderRiskComponentCompact('Variance', riskAssessment.component_scores.rank_variance)}
                    ${renderRiskComponentCompact('Injury', riskAssessment.component_scores.injury)}
                    ${renderRiskComponentCompact('Experience', riskAssessment.component_scores.experience)}
                    ${renderRiskComponentCompact('ADP Gap', riskAssessment.component_scores.adp_ecr)}
                </div>
            `;
        } else {
            riskContent.innerHTML = `
                <div class="text-center text-gray-500 py-4">
                    Risk data unavailable
                </div>
            `;
        }

        // Separate rankings into ADPs and Rankings
        const allRankings = player.rankings || [];
        const adpSources = allRankings.filter(r => r.adp != null);
        const rankSources = allRankings.filter(r => r.overall_rank != null);

        // FantasyPros avg_rank as expert consensus reference (visible even when no community ADP exists)
        const ecrSources = allRankings.filter(r =>
            r.adp == null &&
            r.avg_rank != null &&
            r.source_name === 'FantasyPros'
        );

        // ADPs by Source
        // Check if ESPN ADP is missing (2026 data not yet available)
        const hasEspnAdp = adpSources.some(r => r.source_name && r.source_name.includes('ESPN'));
        const espnNote = !hasEspnAdp ? `
            <div class="flex justify-between items-center py-1.5 border-b border-gray-600/50 text-gray-500 italic">
                <span class="text-xs">ESPN ADP</span>
                <span class="text-xs">2026 data pending</span>
            </div>
        ` : '';

        const adpHtml = adpSources.map(r => `
            <div class="flex justify-between items-center py-1.5 border-b border-gray-600/50 last:border-0">
                <a href="${r.source_url || '#'}" target="_blank" class="source-link text-xs text-purple-300 hover:text-purple-200">${r.source_name}</a>
                <span class="font-bold text-purple-400">${r.adp.toFixed(1)}</span>
            </div>
        `).join('');

        const ecrHtml = ecrSources.map(r => `
            <div class="flex justify-between items-center py-1.5 border-b border-gray-600/50 last:border-0">
                <span class="text-xs text-cyan-300">Expert Rank (ECR)</span>
                <span class="font-bold text-cyan-400">#${Math.round(r.avg_rank)}</span>
            </div>
        `).join('');

        const hasAnyAdp = adpSources.length > 0 || ecrSources.length > 0;
        document.getElementById('modal-adps').innerHTML = hasAnyAdp ?
            adpHtml + ecrHtml + espnNote
            : espnNote + '<p class="text-gray-500 text-center py-4">No ADP data available yet</p>';

        // Rankings by Source
        document.getElementById('modal-rankings').innerHTML = rankSources.length > 0 ? rankSources.map(r => {
            let extraInfo = '';
            if (r.best_rank && r.worst_rank) {
                extraInfo = `<span class="text-gray-500 text-xs">(${r.best_rank}-${r.worst_rank})</span>`;
            }
            return `
                <div class="flex justify-between items-center py-1.5 border-b border-gray-600/50 last:border-0">
                    <a href="${r.source_url || '#'}" target="_blank" class="source-link text-xs text-cyan-300 hover:text-cyan-200">${r.source_name}</a>
                    <div class="flex items-center gap-2">
                        <span class="font-bold text-cyan-400">#${r.overall_rank}</span>
                        ${extraInfo}
                    </div>
                </div>
            `;
        }).join('') : '<p class="text-gray-500 text-center py-4">No ranking data</p>';

        // Projections
        const projections = player.projections || [];
        const playerPos = player.primary_position || '';
        const isHitter = !['SP', 'RP', 'P'].includes(playerPos);

        const fmt = (val, decimals = 0) => {
            if (val === null || val === undefined) return '--';
            return decimals > 0 ? val.toFixed(decimals) : Math.round(val);
        };

        // Split into forward-looking projections (current/future year) vs historical stats (prior year)
        const currentYear = new Date().getFullYear();
        const projRows  = projections.filter(p => !p.projection_year || p.projection_year >= currentYear);
        const statsRows = projections.filter(p => p.projection_year && p.projection_year < currentYear);

        const buildHitterTable = (rows, colorClass) => `
            <div class="overflow-x-auto">
                <table class="w-full text-xs">
                    <thead class="text-gray-400">
                        <tr>
                            <th class="text-left py-1 pr-2">Source</th>
                            <th class="text-center px-2">PA</th>
                            <th class="text-center px-2">HR</th>
                            <th class="text-center px-2">R</th>
                            <th class="text-center px-2">RBI</th>
                            <th class="text-center px-2">SB</th>
                            <th class="text-center px-2">AVG</th>
                            <th class="text-center px-2">OPS</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${rows.filter(p => p.hr > 0 || p.pa > 0).map(p => `
                            <tr class="border-t border-gray-600/50">
                                <td class="py-1.5 pr-2 ${colorClass} font-medium">${p.source_name || 'Unknown'}</td>
                                <td class="text-center px-2">${fmt(p.pa)}</td>
                                <td class="text-center px-2 font-bold text-yellow-400">${fmt(p.hr)}</td>
                                <td class="text-center px-2">${fmt(p.runs)}</td>
                                <td class="text-center px-2">${fmt(p.rbi)}</td>
                                <td class="text-center px-2 text-cyan-400">${fmt(p.sb)}</td>
                                <td class="text-center px-2">${fmt(p.avg, 3)}</td>
                                <td class="text-center px-2">${fmt(p.ops, 3)}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        `;

        const buildPitcherTable = (rows, colorClass) => `
            <div class="overflow-x-auto">
                <table class="w-full text-xs">
                    <thead class="text-gray-400">
                        <tr>
                            <th class="text-left py-1 pr-2">Source</th>
                            <th class="text-center px-2">IP</th>
                            <th class="text-center px-2">W</th>
                            <th class="text-center px-2">K</th>
                            <th class="text-center px-2">SV</th>
                            <th class="text-center px-2">ERA</th>
                            <th class="text-center px-2">WHIP</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${rows.filter(p => p.strikeouts > 0 || p.era > 0 || p.ip > 0).map(p => `
                            <tr class="border-t border-gray-600/50">
                                <td class="py-1.5 pr-2 ${colorClass} font-medium">${p.source_name || 'Unknown'}</td>
                                <td class="text-center px-2">${fmt(p.ip)}</td>
                                <td class="text-center px-2">${fmt(p.wins)}</td>
                                <td class="text-center px-2 font-bold text-yellow-400">${fmt(p.strikeouts)}</td>
                                <td class="text-center px-2 text-cyan-400">${fmt(p.saves)}</td>
                                <td class="text-center px-2">${fmt(p.era, 2)}</td>
                                <td class="text-center px-2">${fmt(p.whip, 2)}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        `;

        // Render 2026 projections section
        if (projRows.length > 0) {
            document.getElementById('modal-projections').innerHTML = isHitter
                ? buildHitterTable(projRows, 'text-emerald-400')
                : buildPitcherTable(projRows, 'text-emerald-400');
        } else {
            document.getElementById('modal-projections').innerHTML = '<p class="text-gray-500 text-center py-4">No projections available</p>';
        }

        // Render 2025 stats section
        const statsSection = document.getElementById('modal-stats-section');
        if (statsRows.length > 0) {
            document.getElementById('modal-stats-history').innerHTML = isHitter
                ? buildHitterTable(statsRows, 'text-amber-400')
                : buildPitcherTable(statsRows, 'text-amber-400');
            statsSection.classList.remove('hidden');
        } else {
            statsSection.classList.add('hidden');
        }

        // Update action bar
        const injuryToggle = document.getElementById('modal-injury-toggle');
        injuryToggle.textContent = player.is_injured ? 'Mark Healthy' : 'Mark Injured';
        injuryToggle.className = `px-3 py-2 text-sm rounded-lg border transition-colors ${
            player.is_injured
                ? 'border-emerald-600 text-emerald-400 hover:bg-emerald-900/30'
                : 'border-gray-600 hover:bg-gray-700'
        }`;

        // Update draft button state
        const draftBtn = document.getElementById('modal-draft-btn');
        if (player.is_drafted) {
            draftBtn.textContent = 'Already Drafted';
            draftBtn.disabled = true;
            draftBtn.className = 'px-4 py-2 text-sm rounded-lg bg-gray-600 text-gray-400 cursor-not-allowed';
        } else {
            draftBtn.textContent = 'Draft Player';
            draftBtn.disabled = false;
            draftBtn.className = 'px-4 py-2 text-sm rounded-lg bg-emerald-600 hover:bg-emerald-500 font-medium transition-colors';
        }

        // Update predict button visibility (only show during active draft)
        updatePredictButton();

    } catch (error) {
        console.error('Failed to load player:', error);
        showModalError(`Failed to load player data: ${error.message}`);
    }
}

// Compact risk component for 2-column grid
function renderRiskComponentCompact(label, component) {
    if (!component) return '';
    const scoreVal = typeof component === 'object' ? component.score : component;
    if (scoreVal === undefined || scoreVal === null || isNaN(scoreVal)) return '';
    const s = Math.round(scoreVal);
    const color = s < 30 ? 'bg-emerald-500' : s < 60 ? 'bg-yellow-500' : 'bg-red-500';
    const detail = (typeof component === 'object' && component.detail) ? component.detail : '';
    return `
        <div class="bg-gray-600/30 rounded p-2" title="${detail}">
            <div class="flex justify-between items-center mb-1">
                <span class="text-xs text-gray-400">${label}</span>
                <span class="text-xs font-medium">${s}</span>
            </div>
            <div class="h-1.5 bg-gray-600 rounded-full overflow-hidden">
                <div class="h-full ${color} rounded-full" style="width: ${s}%"></div>
            </div>
        </div>
    `;
}

// Toggle injury from modal
async function toggleModalInjury() {
    if (!currentModalPlayer) return;
    await toggleInjury(currentModalPlayer.id, currentModalPlayer.is_injured);
    // Refresh modal
    await showPlayerDetail(currentModalPlayer.id);
}

// Toggle notes edit mode from modal
function toggleNotesEdit() {
    const display = document.getElementById('modal-notes-display');
    const edit = document.getElementById('modal-notes-edit');
    const textarea = document.getElementById('modal-notes-textarea');
    const isEditing = !edit.classList.contains('hidden');

    if (isEditing) {
        edit.classList.add('hidden');
        if (currentModalPlayer.custom_notes) display.classList.remove('hidden');
    } else {
        textarea.value = currentModalPlayer.custom_notes || '';
        display.classList.add('hidden');
        edit.classList.remove('hidden');
        textarea.focus();
    }
}

function cancelNotesEdit() {
    document.getElementById('modal-notes-edit').classList.add('hidden');
    if (currentModalPlayer.custom_notes) {
        document.getElementById('modal-notes-display').classList.remove('hidden');
    }
}

async function savePlayerNotes() {
    if (!currentModalPlayer) return;
    const notes = document.getElementById('modal-notes-textarea').value.trim();
    try {
        await fetchAPI(`/players/${currentModalPlayer.id}/notes`, {
            method: 'PUT',
            body: JSON.stringify({ notes }),
        });
        localNotesSet(currentModalPlayer.id, notes);
        await showPlayerDetail(currentModalPlayer.id);
    } catch (error) {
        console.error('Failed to save notes:', error);
        alert('Failed to save notes.');
    }
}

function localNotesGet(playerId) {
    try { return (JSON.parse(localStorage.getItem('fbb_notes') || '{}')[playerId]) || null; }
    catch { return null; }
}

function localNotesSet(playerId, text) {
    try {
        const map = JSON.parse(localStorage.getItem('fbb_notes') || '{}');
        if (text) map[playerId] = text; else delete map[playerId];
        localStorage.setItem('fbb_notes', JSON.stringify(map));
    } catch {}
}

function restoreLocalNote() {
    if (!currentModalPlayer) return;
    const localNote = localNotesGet(currentModalPlayer.id);
    if (!localNote) return;
    document.getElementById('modal-notes-textarea').value = localNote;
    document.getElementById('modal-notes-edit').classList.remove('hidden');
    document.getElementById('modal-notes-display').classList.add('hidden');
    const banner = document.getElementById('notes-restore-banner');
    if (banner) banner.remove();
}

async function exportNotes() {
    const withNotes = players.filter(p => p.custom_notes);
    const payload = withNotes.map(p => ({ id: p.id, name: p.name, note: p.custom_notes }));
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `fbb-notes-${new Date().toISOString().slice(0,10)}.json`;
    a.click();
}

async function importNotes(file) {
    if (!file) return;
    try {
        const text = await file.text();
        const rows = JSON.parse(text);
        for (const row of rows) {
            await fetchAPI(`/players/${row.id}/notes`, {
                method: 'PUT', body: JSON.stringify({ notes: row.note })
            });
            localNotesSet(row.id, row.note);
        }
        alert(`Imported ${rows.length} notes.`);
    } catch (err) {
        console.error('Import failed:', err);
        alert('Import failed. Please check the file format.');
    }
}

// Draft player - assigns to team on the clock automatically
async function draftPlayer(playerId) {
    // Determine if it's the user's pick or another team's pick
    const isMyPick = draftSessionId ? draftIsUserPick : false;
    const teamPosition = draftSessionId ? draftTeamOnClock : null;

    // Call markDrafted with appropriate parameters
    // - If it's user's pick: my_team=true
    // - If it's another team's pick: assign to team on clock
    await markDrafted(playerId, isMyPick, isMyPick ? null : teamPosition);
}

// Draft player from modal
async function draftFromModal() {
    if (!currentModalPlayer || currentModalPlayer.is_drafted) return;
    await draftPlayer(currentModalPlayer.id);
    closeModal();
}

// Close modal
function closeModal() {
    document.getElementById('player-modal').classList.add('hidden');
    // Also hide prediction result when closing
    const predictionResult = document.getElementById('prediction-result');
    if (predictionResult) {
        predictionResult.classList.add('hidden');
    }
}

// Show modal loading state
// Displays the loading spinner and hides modal content
function showModalLoading() {
    const modal = document.getElementById('player-modal');
    const loadingEl = document.getElementById('modal-loading');

    if (modal && loadingEl) {
        modal.classList.remove('hidden');
        loadingEl.classList.remove('hidden');
    }
}

// Hide modal loading state
// Hides the loading spinner to reveal modal content
function hideModalLoading() {
    const loadingEl = document.getElementById('modal-loading');
    if (loadingEl) {
        loadingEl.classList.add('hidden');
    }
}

// Show modal error state
// Displays an error message in the modal when data loading fails
function showModalError(message) {
    const modal = document.getElementById('player-modal');
    const loadingEl = document.getElementById('modal-loading');
    const contentEl = document.querySelector('#player-modal .overflow-y-auto');

    if (modal && contentEl) {
        // Hide loading
        if (loadingEl) {
            loadingEl.classList.add('hidden');
        }

        // Show error in content area
        contentEl.innerHTML = `
            <div class="flex flex-col items-center justify-center py-12 text-center">
                <svg class="w-12 h-12 text-red-500 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                </svg>
                <h3 class="text-lg font-bold text-red-400 mb-2">Failed to Load Player Data</h3>
                <p class="text-sm text-gray-400 mb-4 max-w-md">${escapeHtml(message)}</p>
                <div class="flex gap-3">
                    <button onclick="closeModal()" class="px-4 py-2 text-sm rounded-lg border border-gray-600 hover:bg-gray-700 transition-colors">
                        Close
                    </button>
                    <button onclick="location.reload()" class="px-4 py-2 text-sm rounded-lg bg-blue-600 hover:bg-blue-500 font-medium transition-colors">
                        Reload Page
                    </button>
                </div>
            </div>
        `;

        // Show modal
        modal.classList.remove('hidden');
    }
}

// Close modal on background click
document.getElementById('player-modal').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeModal();
});

// ============================================================================
// Pick Prediction Functions
// ============================================================================

/**
 * Calculate the user's next pick in a snake draft.
 * Returns the overall pick number, or null if no picks remain.
 */
function calculateNextUserPick() {
    if (!draftSessionId) return null;

    // Maximum picks (23 rounds typical for fantasy baseball)
    const maxPicks = draftNumTeams * 23;

    for (let pick = draftCurrentPick; pick <= maxPicks; pick++) {
        const round = Math.floor((pick - 1) / draftNumTeams) + 1;
        const pickInRound = ((pick - 1) % draftNumTeams) + 1;

        // Snake logic: even rounds reverse order
        const teamPicking = (round % 2 === 0)
            ? (draftNumTeams - pickInRound + 1)
            : pickInRound;

        if (teamPicking === draftUserPosition) {
            return pick;
        }
    }

    return null;
}

/**
 * Update the predict button visibility based on draft session state.
 * Called when modal opens or draft session changes.
 */
function updatePredictButton() {
    const btn = document.getElementById('modal-predict-btn');
    if (!btn) return;

    // Only show during active draft session and if player isn't drafted
    const shouldShow = draftSessionId && currentModalPlayer && !currentModalPlayer.is_drafted;
    btn.classList.toggle('hidden', !shouldShow);

    // Also hide/reset prediction result
    const predictionResult = document.getElementById('prediction-result');
    if (predictionResult && !shouldShow) {
        predictionResult.classList.add('hidden');
    }
}

/**
 * Predict player availability using Monte Carlo simulation.
 * Fetches prediction from API and displays result.
 */
async function predictPlayerAvailability() {
    if (!currentModalPlayer || !draftSessionId) return;

    const btn = document.getElementById('modal-predict-btn');
    const predictionResult = document.getElementById('prediction-result');

    // Calculate user's next pick
    const nextPick = calculateNextUserPick();
    if (!nextPick) {
        showPredictionError('No more picks remaining in this draft');
        return;
    }

    // If it's currently the user's pick, show 100% available
    if (nextPick === draftCurrentPick) {
        showPredictionResult({
            probability: 1.0,
            probability_pct: '100%',
            target_pick: nextPick,
            picks_between: 0,
            verdict: 'Available Now',
            confidence: 'High',
            expected_draft_position: currentModalPlayer.consensus_rank || 0,
            volatility_score: 0
        });
        return;
    }

    // Show loading state
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `
            <span class="flex items-center gap-1">
                <svg class="animate-spin w-4 h-4" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Simulating...
            </span>
        `;
    }

    try {
        const response = await fetchAPI(
            `/players/${currentModalPlayer.id}/pick-prediction?` +
            `target_pick=${nextPick}&current_pick=${draftCurrentPick}&num_teams=${draftNumTeams}`
        );

        showPredictionResult(response);
    } catch (error) {
        console.error('Prediction failed:', error);
        showPredictionError('Failed to run prediction simulation');
    } finally {
        // Restore button
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = `
                <span class="flex items-center gap-1">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"></path>
                    </svg>
                    Predict
                </span>
            `;
        }
    }
}

/**
 * Display prediction result in the modal.
 */
function showPredictionResult(prediction) {
    const resultEl = document.getElementById('prediction-result');
    if (!resultEl) return;

    // Determine colors based on probability
    const prob = prediction.probability;
    let bgColor, textColor, borderColor, emoji;

    if (prob >= 0.7) {
        bgColor = 'bg-emerald-900/40';
        textColor = 'text-emerald-400';
        borderColor = 'border-emerald-700/50';
        emoji = '✅';
    } else if (prob >= 0.3) {
        bgColor = 'bg-yellow-900/40';
        textColor = 'text-yellow-400';
        borderColor = 'border-yellow-700/50';
        emoji = '⚠️';
    } else {
        bgColor = 'bg-red-900/40';
        textColor = 'text-red-400';
        borderColor = 'border-red-700/50';
        emoji = '❌';
    }

    const playerName = currentModalPlayer ? currentModalPlayer.name : 'Player';

    resultEl.innerHTML = `
        <div class="${bgColor} border ${borderColor} rounded-lg p-4">
            <div class="flex items-start justify-between gap-4">
                <div class="flex-1">
                    <div class="flex items-center gap-2 mb-2">
                        <span class="text-lg">${emoji}</span>
                        <span class="text-3xl font-bold ${textColor}">${prediction.probability_pct}</span>
                        <span class="text-gray-400 text-sm">chance available</span>
                    </div>
                    <p class="text-sm text-gray-300 mb-3">
                        <strong class="text-white">${playerName}</strong>
                        ${prediction.picks_between > 0
                            ? `will survive <strong class="text-white">${prediction.picks_between} picks</strong> to reach pick <strong class="text-white">#${prediction.target_pick}</strong>`
                            : 'is available for you to draft now'
                        }
                    </p>
                    <div class="grid grid-cols-3 gap-3 text-xs">
                        <div>
                            <span class="text-gray-500">Expected Pick</span>
                            <div class="font-bold text-gray-300">#${Math.round(prediction.expected_draft_position)}</div>
                        </div>
                        <div>
                            <span class="text-gray-500">Volatility</span>
                            <div class="font-bold text-gray-300">${prediction.volatility_score.toFixed(1)}</div>
                        </div>
                        <div>
                            <span class="text-gray-500">Confidence</span>
                            <div class="font-bold ${prediction.confidence === 'High' ? 'text-emerald-400' : prediction.confidence === 'Medium' ? 'text-yellow-400' : 'text-red-400'}">${prediction.confidence}</div>
                        </div>
                    </div>
                </div>
                <div class="text-right">
                    <div class="text-xs px-2 py-1 rounded ${bgColor} ${textColor} font-medium">${prediction.verdict.toUpperCase()}</div>
                    ${prediction.simulations_run ? `<div class="text-xs text-gray-500 mt-1">${prediction.simulations_run.toLocaleString()} sims</div>` : ''}
                </div>
            </div>
        </div>
    `;

    resultEl.classList.remove('hidden');
}

/**
 * Show prediction error message.
 */
function showPredictionError(message) {
    const resultEl = document.getElementById('prediction-result');
    if (!resultEl) return;

    resultEl.innerHTML = `
        <div class="bg-red-900/30 border border-red-700/50 rounded-lg p-3">
            <div class="flex items-center gap-2 text-red-400">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                </svg>
                <span class="text-sm">${message}</span>
            </div>
        </div>
    `;

    resultEl.classList.remove('hidden');
}

// Mark player as drafted
async function markDrafted(playerId, myTeam = false, teamDraftPosition = null) {
    try {
        // Find the player before drafting (for achievement checks)
        const player = players.find(p => p.id === playerId);

        // Build URL with session_id if session is active
        let url = `/players/${playerId}/draft?my_team=${myTeam}`;
        if (draftSessionId) {
            url += `&session_id=${draftSessionId}`;
        }
        if (teamDraftPosition !== null) {
            url += `&team_id=${teamDraftPosition}`;
        }

        // Call API to mark player as drafted
        const result = await fetchAPI(url, { method: 'POST' });

        // Track picks for achievements
        if (draftSessionId && myTeam) {
            sessionPickCount++;
            myTeamPickCount++;
            checkAchievements(player);
            updateXPBar();
        }

        // Update session state FIRST so draftIsUserPick is correct before re-rendering
        if (draftSessionId) {
            await updateSessionState();
        }

        // Reload player list — renderPlayerList() now uses fresh draftIsUserPick
        await loadPlayers();

        // Reload recommendations
        await loadRecommendations(); await loadScarcity();

        // If it's my pick, update My Team tab count
        if (myTeam) {
            updateMyTeamCount();
        }

        // Mark unsaved changes
        if (draftSessionId) {
            markUnsavedChanges();
        }
    } catch (error) {
        console.error('Failed to mark player as drafted:', error);
        showError('Failed to mark player as drafted');
    }
}

// Update My Team tab with roster count
async function updateMyTeamCount() {
    try {
        const roster = await fetchAPI(withUserKey(`/players/my-team/roster?league_id=${currentLeagueId}`));
        const tabBtn = document.querySelector('[data-tab="my-team"]');
        if (tabBtn) {
            tabBtn.textContent = `My Team (${roster.length})`;
        }
    } catch (error) {
        console.error('Failed to update team count:', error);
    }
}

// Search players
function searchPlayers(query) {
    searchQuery = query;
    renderPlayerList();

    // Show/hide clear button
    const clearBtn = document.getElementById('search-clear');
    if (clearBtn) {
        clearBtn.classList.toggle('hidden', !query);
    }

    // Mark unsaved changes when UI preferences are modified
    markUnsavedChanges();
}

// Clear search
function clearSearch() {
    const searchInput = document.getElementById('player-search');
    if (searchInput) {
        searchInput.value = '';
        searchPlayers('');
        searchInput.focus();
    }
}

// Filter by position (multi-select toggle)
function togglePosition(btn) {
    const pos = btn.dataset.pos;

    if (pos === '') {
        // "All" button — clear all selections
        selectedPositions.clear();
        document.querySelectorAll('.pos-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
    } else {
        // Deactivate "All" button
        document.querySelector('.pos-btn[data-pos=""]').classList.remove('active');

        if (selectedPositions.has(pos)) {
            selectedPositions.delete(pos);
            btn.classList.remove('active');
        } else {
            selectedPositions.add(pos);
            btn.classList.add('active');
        }

        // If nothing selected, re-activate "All"
        if (selectedPositions.size === 0) {
            document.querySelector('.pos-btn[data-pos=""]').classList.add('active');
        }
    }

    renderPlayerList();

    // Mark unsaved changes when UI preferences are modified
    markUnsavedChanges();
}

// Clear all filters (position, team, search)
function clearAllFilters() {
    // Reset positions
    selectedPositions.clear();
    document.querySelectorAll('.pos-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('.pos-btn[data-pos=""]').classList.add('active');

    // Reset team
    currentTeam = '';
    document.getElementById('team-filter').value = '';

    // Reset search
    searchQuery = '';
    const searchInput = document.getElementById('player-search');
    if (searchInput) searchInput.value = '';
    const clearBtn = document.getElementById('search-clear');
    if (clearBtn) clearBtn.classList.add('hidden');

    renderPlayerList();
}

// Filter by team
function filterByTeam(team) {
    currentTeam = team;
    renderPlayerList();

    // Mark unsaved changes when UI preferences are modified
    markUnsavedChanges();
}

// Tab switching
function showTab(tabName) {
    // Hide all tabs
    document.querySelectorAll('.tab-content').forEach(tab => tab.classList.add('hidden'));
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('active');
        btn.setAttribute('aria-selected', 'false');
    });

    // Show selected tab
    document.getElementById(`tab-${tabName}`).classList.remove('hidden');
    const activeBtn = document.querySelector(`[data-tab="${tabName}"]`);
    activeBtn.classList.add('active');
    activeBtn.setAttribute('aria-selected', 'true');

    // Load tab-specific data
    if (tabName === 'my-team') {
        loadMyTeam();
    } else if (tabName === 'draft-history') {
        loadDraftHistory();
    } else if (tabName === 'teams') {
        loadTeamsTabConfig();
        updateTeamSelector();
        loadClaimableTeams();
    } else if (tabName === 'keepers') {
        loadKeepers();
    }
}

// Load category strengths
async function loadCategories() {
    if (!currentLeagueId) return;

    try {
        const needs = await fetchAPI(withUserKey(`/recommendations/${currentLeagueId}/needs`));
        renderCategoryBars(needs);
    } catch (error) {
        document.getElementById('category-bars').innerHTML = `
            <div class="text-center py-8">
                <div class="text-4xl mb-3 opacity-50">📊</div>
                <p class="text-gray-400">Category analysis unavailable</p>
                <p class="text-sm text-gray-500 mt-1">Draft some players to see your team's category strengths</p>
            </div>
        `;
    }
}

// Render category bars
function renderCategoryBars(data) {
    const battingCategories = [
        { key: 'runs', label: 'Runs', short: 'R' },
        { key: 'hr', label: 'Home Runs', short: 'HR' },
        { key: 'rbi', label: 'RBI', short: 'RBI' },
        { key: 'sb', label: 'Stolen Bases', short: 'SB' },
        { key: 'avg', label: 'Batting Avg', short: 'AVG' },
        { key: 'ops', label: 'OPS', short: 'OPS' },
    ];

    const pitchingCategories = [
        { key: 'wins', label: 'Wins', short: 'W' },
        { key: 'strikeouts', label: 'Strikeouts', short: 'K' },
        { key: 'era', label: 'ERA', short: 'ERA', inverted: true },
        { key: 'whip', label: 'WHIP', short: 'WHIP', inverted: true },
        { key: 'saves', label: 'Saves', short: 'SV' },
        { key: 'quality_starts', label: 'Quality Starts', short: 'QS' },
    ];

    const strengths = data.strengths || {};

    const renderCategory = (cat, index) => {
        const value = strengths[cat.key] || 50;
        const colorClass = value >= 70 ? 'excellent' : value >= 50 ? 'good' : value >= 30 ? 'average' : 'poor';
        const textColor = value >= 70 ? 'text-emerald-400' : value >= 50 ? 'text-blue-400' : value >= 30 ? 'text-yellow-400' : 'text-red-400';
        const icon = value >= 70 ? '✓' : value >= 50 ? '○' : value >= 30 ? '△' : '✕';

        return `
            <div class="category-item animate-slide-up" style="animation-delay: ${index * 50}ms">
                <div class="flex justify-between items-center mb-2">
                    <div class="flex items-center gap-2">
                        <span class="category-icon ${colorClass}">${icon}</span>
                        <span class="text-sm font-medium">${cat.label}</span>
                        <span class="category-short">${cat.short}</span>
                    </div>
                    <span class="category-value ${textColor}">${Math.round(value)}%</span>
                </div>
                <div class="category-bar">
                    <div class="category-bar-fill ${colorClass}" style="width: ${value}%"></div>
                </div>
            </div>
        `;
    };

    document.getElementById('category-bars').innerHTML = `
        <div class="category-section">
            <div class="flex items-center gap-2 mb-4">
                <span class="category-section-icon">⚾</span>
                <h4 class="font-semibold text-emerald-400">Batting</h4>
            </div>
            <div class="space-y-4">
                ${battingCategories.map((cat, i) => renderCategory(cat, i)).join('')}
            </div>
        </div>
        <div class="category-section mt-6">
            <div class="flex items-center gap-2 mb-4">
                <span class="category-section-icon">🎯</span>
                <h4 class="font-semibold text-red-400">Pitching</h4>
            </div>
            <div class="space-y-4">
                ${pitchingCategories.map((cat, i) => renderCategory(cat, i + battingCategories.length)).join('')}
            </div>
        </div>
    `;
}

// Load my team
async function loadMyTeam() {
    try {
        const roster = await fetchAPI(withUserKey(`/players/my-team/roster?league_id=${currentLeagueId}`));

        if (roster.length === 0) {
            document.getElementById('my-roster').innerHTML = `
                <div class="text-center py-8">
                    <div class="text-4xl mb-3 opacity-50">⚾</div>
                    <p class="text-gray-400">No players drafted yet</p>
                    <p class="text-sm text-gray-500 mt-1">Click "✓ Mine" to add players to your team</p>
                </div>
            `;
            return;
        }

        // Group by position
        const hitters = roster.filter(p => !['SP', 'RP'].includes(p.primary_position));
        const pitchers = roster.filter(p => ['SP', 'RP'].includes(p.primary_position));

        document.getElementById('my-roster').innerHTML = `
            <div class="mb-6">
                <div class="flex items-center gap-2 mb-3">
                    <span class="roster-section-icon hitter">⚾</span>
                    <h4 class="font-semibold text-gray-200">Hitters</h4>
                    <span class="roster-count">${hitters.length}</span>
                </div>
                <div class="space-y-2">
                    ${hitters.map((p, i) => `
                        <div class="roster-player-card animate-slide-up" style="animation-delay: ${i * 50}ms">
                            <div class="flex items-center gap-3">
                                <button onclick="showPlayerDetail(${p.id})" class="font-medium hover:text-emerald-400 transition-colors">
                                    ${p.name}
                                </button>
                                ${renderPositionBadge(p.positions)}
                            </div>
                            <div class="flex items-center gap-3">
                                <span class="text-gray-400 text-sm">${p.team}</span>
                                <button onclick="undraftPlayer(${p.id})" class="remove-btn" title="Remove from roster">
                                    ✕
                                </button>
                            </div>
                        </div>
                    `).join('')}
                </div>
            </div>
            <div class="mb-6">
                <div class="flex items-center gap-2 mb-3">
                    <span class="roster-section-icon pitcher">🎯</span>
                    <h4 class="font-semibold text-gray-200">Pitchers</h4>
                    <span class="roster-count">${pitchers.length}</span>
                </div>
                <div class="space-y-2">
                    ${pitchers.map((p, i) => `
                        <div class="roster-player-card animate-slide-up" style="animation-delay: ${(hitters.length + i) * 50}ms">
                            <div class="flex items-center gap-3">
                                <button onclick="showPlayerDetail(${p.id})" class="font-medium hover:text-emerald-400 transition-colors">
                                    ${p.name}
                                </button>
                                ${renderPositionBadge(p.primary_position)}
                            </div>
                            <div class="flex items-center gap-3">
                                <span class="text-gray-400 text-sm">${p.team}</span>
                                <button onclick="undraftPlayer(${p.id})" class="remove-btn" title="Remove from roster">
                                    ✕
                                </button>
                            </div>
                        </div>
                    `).join('')}
                </div>
            </div>
            <div class="roster-summary">
                <div class="flex justify-between items-center">
                    <span class="text-gray-400">Total Roster</span>
                    <span class="text-lg font-bold text-emerald-400">${roster.length} players</span>
                </div>
            </div>
        `;
    } catch (error) {
        console.error('Failed to load roster:', error);
        document.getElementById('my-roster').innerHTML = `
            <div class="text-center py-8">
                <div class="text-4xl mb-3">⚠️</div>
                <p class="text-red-400">Failed to load roster</p>
            </div>
        `;
    }
}

// ============================================================================
// Draft History Tab Functions
// ============================================================================

// Load draft history into the History tab
async function loadDraftHistory() {
    const listEl = document.getElementById('draft-history-list');
    const countEl = document.getElementById('history-pick-count');

    if (!draftSessionId) {
        listEl.innerHTML = `
            <div class="text-center py-8">
                <div class="text-4xl mb-3 opacity-50">📋</div>
                <p class="text-gray-400">No active draft session</p>
                <p class="text-sm text-gray-500 mt-1">Start a draft session to track pick history</p>
            </div>
        `;
        countEl.textContent = '';
        return;
    }

    listEl.innerHTML = '<div class="text-center py-4 text-gray-400">Loading history...</div>';

    try {
        const response = await fetchAPI(
            `/draft/session/history?session_id=${draftSessionId}&include_undone=true`
        );

        if (response.history.length === 0) {
            listEl.innerHTML = `
                <div class="text-center py-8">
                    <div class="text-4xl mb-3 opacity-50">📋</div>
                    <p class="text-gray-400">No picks recorded yet</p>
                    <p class="text-sm text-gray-500 mt-1">Picks will appear here as the draft progresses</p>
                </div>
            `;
            countEl.textContent = '0 picks';
            return;
        }

        // Count non-undone picks
        const activePicks = response.history.filter(p => !p.is_undone).length;
        countEl.textContent = `${activePicks} picks`;

        // Group by round for better organization
        const picksByRound = {};
        response.history.forEach(pick => {
            const round = Math.floor((pick.sequence_num - 1) / draftNumTeams) + 1;
            if (!picksByRound[round]) {
                picksByRound[round] = [];
            }
            picksByRound[round].push(pick);
        });

        let html = '';
        Object.keys(picksByRound).sort((a, b) => b - a).forEach(round => {
            const picks = picksByRound[round];
            html += `
                <div class="mb-4">
                    <div class="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">Round ${round}</div>
                    <div class="space-y-1">
                        ${picks.map(pick => {
                            const isUndone = pick.is_undone;
                            const isMyPick = pick.action === 'draft_mine';
                            const teamName = pick.team_name || (isMyPick ? 'My Team' : 'Other');

                            let bgClass = 'bg-gray-700/30';
                            let textClass = 'text-gray-300';
                            if (isUndone) {
                                bgClass = 'bg-gray-900/30';
                                textClass = 'text-gray-500 line-through';
                            } else if (isMyPick) {
                                bgClass = 'bg-emerald-900/20 border border-emerald-800/30';
                                textClass = 'text-emerald-300';
                            }

                            return `
                                <div class="flex items-center justify-between p-2 rounded ${bgClass}">
                                    <div class="flex items-center gap-2">
                                        <span class="text-xs font-mono text-gray-500 w-6">${pick.sequence_num}</span>
                                        <span class="${textClass} text-sm">${pick.player_name}</span>
                                    </div>
                                    <div class="flex items-center gap-2">
                                        <span class="text-xs ${isMyPick ? 'text-emerald-400' : 'text-gray-500'}">${teamName}</span>
                                        ${isUndone ? '<span class="text-xs text-yellow-500">(undone)</span>' : ''}
                                    </div>
                                </div>
                            `;
                        }).join('')}
                    </div>
                </div>
            `;
        });

        listEl.innerHTML = html;

    } catch (error) {
        console.error('Failed to load draft history:', error);
        listEl.innerHTML = `
            <div class="text-center py-8">
                <div class="text-4xl mb-3">⚠️</div>
                <p class="text-red-400">Failed to load history</p>
            </div>
        `;
    }
}

// ============================================================================
// Teams Tab Functions
// ============================================================================

// Update the team selector dropdown
function updateTeamSelector() {
    const selector = document.getElementById('team-selector');
    const rosterEl = document.getElementById('selected-team-roster');

    if (!draftSessionId || draftTeams.length === 0) {
        selector.innerHTML = '<option value="">-- No active draft --</option>';
        rosterEl.innerHTML = `
            <div class="text-center py-8">
                <div class="text-4xl mb-3 opacity-50">👥</div>
                <p class="text-gray-400">No active draft session</p>
                <p class="text-sm text-gray-500 mt-1">Start a draft session to view team rosters</p>
            </div>
        `;
        return;
    }

    // Build team options
    const currentValue = selector.value;
    selector.innerHTML = `
        <option value="">-- Select a team --</option>
        ${draftTeams.map(team => `
            <option value="${team.draft_position}" ${team.is_user_team ? 'class="text-emerald-400"' : ''}>
                ${team.name}${team.is_user_team ? ' (You)' : ''}
            </option>
        `).join('')}
    `;

    // Restore previous selection if still valid
    if (currentValue) {
        selector.value = currentValue;
        loadSelectedTeamRoster();
    } else {
        rosterEl.innerHTML = `
            <div class="text-center py-8">
                <div class="text-4xl mb-3 opacity-50">👥</div>
                <p class="text-gray-400">Select a team to view their roster</p>
            </div>
        `;
    }
}

// Load the roster for the selected team
async function loadSelectedTeamRoster() {
    const selector = document.getElementById('team-selector');
    const rosterEl = document.getElementById('selected-team-roster');
    const teamPosition = parseInt(selector.value);

    if (!teamPosition || !draftSessionId) {
        rosterEl.innerHTML = `
            <div class="text-center py-8">
                <div class="text-4xl mb-3 opacity-50">👥</div>
                <p class="text-gray-400">Select a team to view their roster</p>
            </div>
        `;
        return;
    }

    rosterEl.innerHTML = '<div class="text-center py-4 text-gray-400">Loading roster...</div>';

    try {
        // Get draft history to find picks by this team
        const response = await fetchAPI(
            `/draft/session/history?session_id=${draftSessionId}&include_undone=false`
        );

        // Filter picks for this team
        const teamPicks = response.history.filter(pick => {
            // Check if team_id matches the draft position
            return pick.team_id === teamPosition ||
                   (pick.action === 'draft_mine' && teamPosition === draftUserPosition);
        });

        const team = draftTeams.find(t => t.draft_position === teamPosition);
        const teamName = team ? team.name : `Team ${teamPosition}`;
        const isUserTeam = team?.is_user_team || teamPosition === draftUserPosition;

        if (teamPicks.length === 0) {
            rosterEl.innerHTML = `
                <div class="text-center py-8">
                    <div class="text-4xl mb-3 opacity-50">📋</div>
                    <p class="text-gray-400">${teamName} has no picks yet</p>
                </div>
            `;
            return;
        }

        // Sort by pick order
        teamPicks.sort((a, b) => a.sequence_num - b.sequence_num);

        rosterEl.innerHTML = `
            <div class="mb-3 flex items-center justify-between">
                <h4 class="font-semibold ${isUserTeam ? 'text-emerald-400' : 'text-gray-200'}">${teamName}</h4>
                <span class="text-sm text-gray-500">${teamPicks.length} players</span>
            </div>
            <div class="space-y-1">
                ${teamPicks.map((pick, i) => `
                    <div class="flex items-center justify-between p-2 rounded ${isUserTeam ? 'bg-emerald-900/20' : 'bg-gray-700/30'} animate-slide-up" style="animation-delay: ${i * 30}ms">
                        <div class="flex items-center gap-2">
                            <span class="text-xs font-mono text-gray-500 w-6">${pick.sequence_num}</span>
                            <span class="text-sm ${isUserTeam ? 'text-emerald-300' : 'text-gray-300'}">${pick.player_name}</span>
                        </div>
                        <div class="text-xs text-gray-500">
                            Rd ${Math.floor((pick.sequence_num - 1) / draftNumTeams) + 1}
                        </div>
                    </div>
                `).join('')}
            </div>
        `;

    } catch (error) {
        console.error('Failed to load team roster:', error);
        rosterEl.innerHTML = `
            <div class="text-center py-8">
                <div class="text-4xl mb-3">⚠️</div>
                <p class="text-red-400">Failed to load roster</p>
            </div>
        `;
    }
}

async function loadClaimableTeams() {
    if (!currentLeagueId) return;
    const selectEl = document.getElementById('claim-team-select');
    const statusEl = document.getElementById('claim-status');
    if (!selectEl || !statusEl) return;

    try {
        const teams = await fetchAPI(withUserKey(`/leagues/${currentLeagueId}/teams`));
        selectEl.innerHTML = '<option value="">-- Select a team to claim --</option>';

        let myClaim = null;
        teams.forEach(team => {
            const opt = document.createElement('option');
            opt.value = String(team.id);
            const taken = team.claimed_by_user && !team.claimed_by_me;
            opt.textContent = `${team.name}${team.claimed_by_me ? ' (Mine)' : taken ? ' (Claimed)' : ''}`;
            opt.disabled = taken;
            if (team.claimed_by_me) myClaim = team;
            selectEl.appendChild(opt);
        });

        if (myClaim) {
            selectEl.value = String(myClaim.id);
            statusEl.textContent = `You claimed: ${myClaim.name}`;
            statusEl.className = 'text-xs text-emerald-300 mb-2';
            setClaimedTeamBadge(myClaim.name);
        } else {
            statusEl.textContent = 'No team claimed yet.';
            statusEl.className = 'text-xs text-gray-400 mb-2';
            setClaimedTeamBadge(null);
        }
    } catch (error) {
        console.error('Failed to load claimable teams:', error);
        statusEl.textContent = 'Unable to load team claims.';
        statusEl.className = 'text-xs text-red-300 mb-2';
    }
}

async function claimSelectedTeam() {
    if (!currentLeagueId) return;
    const selectEl = document.getElementById('claim-team-select');
    const teamId = parseInt(selectEl?.value || '');
    if (!teamId) {
        alert('Select a team to claim first.');
        return;
    }

    try {
        await fetchAPI(`/leagues/${currentLeagueId}/claim-team`, {
            method: 'POST',
            body: JSON.stringify({ team_id: teamId, user_key: currentUserKey }),
        });
        await loadClaimableTeams();
        await loadRecommendations();
        await loadMyTeam();
        updateMyTeamCount();
        showNotification('Team claimed', 'success');
    } catch (error) {
        alert(`Failed to claim team: ${error.message}`);
    }
}

async function releaseMyTeamClaim() {
    if (!currentLeagueId) return;
    try {
        await fetchAPI(`/leagues/${currentLeagueId}/claim-team?user_key=${encodeURIComponent(currentUserKey || '')}`, {
            method: 'DELETE',
        });
        await loadClaimableTeams();
        await loadRecommendations();
        await loadMyTeam();
        updateMyTeamCount();
        showNotification('Team claim released', 'success');
    } catch (error) {
        alert(`Failed to release claim: ${error.message}`);
    }
}

// Undraft a player (remove from my team)
async function undraftPlayer(playerId) {
    try {
        await fetchAPI(`/players/${playerId}/undraft`, { method: 'POST' });
        await loadPlayers();
        await loadMyTeam();
        await loadRecommendations(); await loadScarcity();
        updateMyTeamCount();
    } catch (error) {
        console.error('Failed to undraft player:', error);
    }
}

// Refresh data and sync with ESPN
async function refreshData() {
    try {
        // Sync with ESPN to get latest draft picks
        if (currentLeagueId) {
            try {
                const syncResult = await fetchAPI(`/draft/${currentLeagueId}/auto-sync`, { method: 'POST' });
                if (syncResult.newly_drafted_count > 0) {
                    console.log(`Synced ${syncResult.newly_drafted_count} new picks from ESPN`);
                }
            } catch (espnError) {
                // ESPN sync failed - might not have credentials, that's OK
                console.log('ESPN sync skipped:', espnError.message);
            }
        }

        // Reload player list and recommendations
        await loadPlayers();
        await loadRecommendations(); await loadScarcity();
        updateLastUpdated();
    } catch (error) {
        console.error('Failed to refresh data:', error);
        showError('Failed to refresh data');
    }
}

// Update last updated timestamp
function updateLastUpdated() {
    document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();
}

// Toggle the sync dropdown (header Zone 3)
function toggleSyncDropdown() {
    const dropdown = document.getElementById('sync-dropdown');
    dropdown.classList.toggle('hidden');
}

// Toggle the draft bar overflow menu (··· button)
function toggleDraftOverflowMenu() {
    const menu = document.getElementById('draft-overflow-menu');
    menu.classList.toggle('hidden');
    // When closing, also close the restore sub-panel
    if (menu.classList.contains('hidden')) {
        const restorePanel = document.getElementById('restore-session-dropdown');
        if (restorePanel) restorePanel.classList.add('hidden');
    }
}

// Close dropdowns when clicking outside
document.addEventListener('click', (e) => {
    if (!e.target.closest('#sync-btn') && !e.target.closest('#sync-dropdown')) {
        const d = document.getElementById('sync-dropdown');
        if (d) d.classList.add('hidden');
    }
    if (!e.target.closest('#draft-overflow-btn') && !e.target.closest('#draft-overflow-menu')) {
        const m = document.getElementById('draft-overflow-menu');
        if (m) {
            m.classList.add('hidden');
            const r = document.getElementById('restore-session-dropdown');
            if (r) r.classList.add('hidden');
        }
    }
});

// Show error message
function showError(message, type = 'error') {
    // Log to console for debugging
    console.error(message);

    // Create visual toast notification
    createErrorToast(message, type);
}

// Show loading indicator in a container
// Displays a loading spinner with optional message for section-specific loading states
function showSectionLoading(containerId, message = 'Loading...') {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = `
            <div class="flex flex-col items-center justify-center py-8">
                <div class="spinner w-6 h-6 mb-2"></div>
                <span class="text-sm text-gray-400">${message}</span>
            </div>
        `;
    }
}

// Show error in a container with enhanced error handling
// Provides user-friendly error messages with context-specific retry functionality
function showSectionError(containerId, message, retryCallback = null) {
    const container = document.getElementById(containerId);
    if (container) {
        // Determine the appropriate retry function based on container
        let retryFunction = 'loadRecommendations()';
        if (containerId.includes('scarcity')) {
            retryFunction = 'loadScarcityData()';
        } else if (containerId.includes('roster')) {
            retryFunction = 'loadMyTeam()';
        } else if (containerId.includes('history')) {
            retryFunction = 'loadDraftHistory()';
        }

        // Use provided callback if available
        if (retryCallback) {
            retryFunction = `${retryCallback.toString()}()`;
        }

        container.innerHTML = `
            <div class="flex flex-col items-center justify-center py-8 text-center">
                <svg class="w-8 h-8 text-red-500 mb-2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                </svg>
                <span class="text-sm text-red-400 mb-2">Failed to load data</span>
                <span class="text-xs text-gray-500 mb-3">${escapeHtml(message)}</span>
                <button onclick="${retryFunction}" class="text-xs bg-red-700 hover:bg-red-600 px-2 py-1 rounded">
                    Retry
                </button>
            </div>
        `;
    }
}

// Enhanced error handler with retry capability
function showErrorWithRetry(message, retryCallback, retryLabel = 'Retry') {
    // Log to console for debugging
    console.error(message);

    // Remove any existing toast with the same message to prevent duplicates
    const existingToasts = document.querySelectorAll('.error-toast');
    existingToasts.forEach(toast => {
        if (toast.querySelector('.error-message').textContent === message) {
            toast.remove();
        }
    });

    // Create toast container if it doesn't exist
    let toastContainer = document.getElementById('toast-container');
    if (!toastContainer) {
        toastContainer = document.createElement('div');
        toastContainer.id = 'toast-container';
        toastContainer.className = 'fixed top-4 right-4 z-50 space-y-2';
        document.body.appendChild(toastContainer);
    }

    // Create toast element
    const toast = document.createElement('div');
    toast.className = `error-toast flex items-start gap-2 p-3 rounded-lg shadow-lg max-w-md animate-fade-in bg-red-900/90 border border-red-700`;

    toast.innerHTML = `
        <svg class="w-5 h-5 text-red-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
        </svg>
        <div class="flex-1">
            <div class="error-message text-sm font-medium">${escapeHtml(message)}</div>
        </div>
        <div class="flex flex-col gap-1">
            <button class="text-xs bg-red-700 hover:bg-red-600 px-2 py-1 rounded" onclick="(${retryCallback.toString()})()">
                ${retryLabel}
            </button>
            <button class="error-toast-close text-gray-400 hover:text-white flex-shrink-0" onclick="this.closest('.error-toast').remove()">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                </svg>
            </button>
        </div>
    `;

    // Add to container
    toastContainer.appendChild(toast);

    // Auto-dismiss after 10 seconds (longer for errors with retry)
    setTimeout(() => {
        if (toast.parentNode) {
            toast.remove();
        }
    }, 10000);
}

// Create error toast notification
function createErrorToast(message, type = 'error') {
    // Remove any existing toast with the same message to prevent duplicates
    const existingToasts = document.querySelectorAll('.error-toast');
    existingToasts.forEach(toast => {
        if (toast.querySelector('.error-message').textContent === message) {
            toast.remove();
        }
    });

    // Create toast container if it doesn't exist
    let toastContainer = document.getElementById('toast-container');
    if (!toastContainer) {
        toastContainer = document.createElement('div');
        toastContainer.id = 'toast-container';
        toastContainer.className = 'fixed top-4 right-4 z-50 space-y-2';
        document.body.appendChild(toastContainer);
    }

    // Create toast element
    const toast = document.createElement('div');
    toast.className = `error-toast flex items-start gap-2 p-3 rounded-lg shadow-lg max-w-md animate-fade-in`;

    // Set styling based on error type
    switch (type) {
        case 'success':
            toast.classList.add('bg-green-900/90', 'border', 'border-green-700');
            break;
        case 'warning':
            toast.classList.add('bg-yellow-900/90', 'border', 'border-yellow-700');
            break;
        case 'info':
            toast.classList.add('bg-blue-900/90', 'border', 'border-blue-700');
            break;
        default: // error
            toast.classList.add('bg-red-900/90', 'border', 'border-red-700');
    }

    // Add icon based on type
    let iconHtml = '';
    switch (type) {
        case 'success':
            iconHtml = '<svg class="w-5 h-5 text-green-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg>';
            break;
        case 'warning':
            iconHtml = '<svg class="w-5 h-5 text-yellow-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>';
            break;
        case 'info':
            iconHtml = '<svg class="w-5 h-5 text-blue-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>';
            break;
        default: // error
            iconHtml = '<svg class="w-5 h-5 text-red-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>';
    }

    toast.innerHTML = `
        ${iconHtml}
        <div class="flex-1">
            <div class="error-message text-sm font-medium">${escapeHtml(message)}</div>
        </div>
        <button class="error-toast-close text-gray-400 hover:text-white flex-shrink-0" onclick="this.closest('.error-toast').remove()">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
            </svg>
        </button>
    `;

    // Add to container
    toastContainer.appendChild(toast);

    // Auto-dismiss after 5 seconds
    setTimeout(() => {
        if (toast.parentNode) {
            toast.remove();
        }
    }, 5000);
}

// Global Loading State Management
function showGlobalLoading() {
    if (!globalLoadingElement) {
        globalLoadingElement = document.createElement('div');
        globalLoadingElement.id = 'global-loading-indicator';
        globalLoadingElement.className = 'fixed top-4 right-4 z-50 flex items-center gap-2 px-3 py-2 bg-gray-800 border border-cyan-500 rounded-lg shadow-lg';
        globalLoadingElement.innerHTML = `
            <div class="spinner w-4 h-4"></div>
            <span class="text-sm text-cyan-400 font-medium">Loading...</span>
        `;
        globalLoadingElement.style.display = 'none';
        document.body.appendChild(globalLoadingElement);
    }

    globalLoadingElement.style.display = 'flex';
}

function hideGlobalLoading() {
    if (globalLoadingElement) {
        globalLoadingElement.style.display = 'none';
    }
}

function incrementLoadingCounter() {
    activeRequests++;
    if (activeRequests > 0) {
        showGlobalLoading();
    }
}

function decrementLoadingCounter() {
    activeRequests = Math.max(0, activeRequests - 1);
    if (activeRequests === 0) {
        hideGlobalLoading();
    }
}

// Section-specific loading functions
function showSectionLoading(containerId, message = 'Loading...') {
    const container = document.getElementById(containerId);
    if (container) {
        container.innerHTML = `
            <div class="flex flex-col items-center justify-center py-8">
                <div class="spinner w-6 h-6 mb-2"></div>
                <span class="text-sm text-gray-400">${message}</span>
            </div>
        `;
    }
}

function hideSectionLoading(containerId) {
    // This function can be used to clear loading state
    // The actual content should be loaded by the calling function
}

// Enhanced error handler with retry capability
function showErrorWithRetry(message, retryCallback, retryLabel = 'Retry') {
    // Log to console for debugging
    console.error(message);

    // Remove any existing toast with the same message to prevent duplicates
    const existingToasts = document.querySelectorAll('.error-toast');
    existingToasts.forEach(toast => {
        if (toast.querySelector('.error-message').textContent === message) {
            toast.remove();
        }
    });

    // Create toast container if it doesn't exist
    let toastContainer = document.getElementById('toast-container');
    if (!toastContainer) {
        toastContainer = document.createElement('div');
        toastContainer.id = 'toast-container';
        toastContainer.className = 'fixed top-4 right-4 z-50 space-y-2';
        document.body.appendChild(toastContainer);
    }

    // Create toast element
    const toast = document.createElement('div');
    toast.className = `error-toast flex items-start gap-2 p-3 rounded-lg shadow-lg max-w-md animate-fade-in bg-red-900/90 border border-red-700`;

    toast.innerHTML = `
        <svg class="w-5 h-5 text-red-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
        </svg>
        <div class="flex-1">
            <div class="error-message text-sm font-medium">${escapeHtml(message)}</div>
        </div>
        <div class="flex flex-col gap-1">
            <button class="text-xs bg-red-700 hover:bg-red-600 px-2 py-1 rounded" onclick="(${retryCallback.toString()})()">
                ${retryLabel}
            </button>
            <button class="error-toast-close text-gray-400 hover:text-white flex-shrink-0" onclick="this.closest('.error-toast').remove()">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                </svg>
            </button>
        </div>
    `;

    // Add to container
    toastContainer.appendChild(toast);

    // Auto-dismiss after 10 seconds (longer for errors with retry)
    setTimeout(() => {
        if (toast.parentNode) {
            toast.remove();
        }
    }, 10000);
}

// Refresh ALL data for draft day preparation
async function refreshAllData() {
    if (!confirm('This will refresh ALL data:\n• ADP (ESPN, FantasyPros, NFBC)\n• Injuries\n• Projections\n• Rankings\n• News\n• Risk Scores\n\nThis may take 1-2 minutes. Continue?')) {
        return;
    }

    // Show loading indicator
    const btn = event.target;
    const originalText = btn.textContent;
    btn.textContent = '⏳ Refreshing...';
    btn.disabled = true;

    try {
        const result = await fetchAPI('/players/refresh-all', {
            method: 'POST',
            timeoutMs: 180000,
            retries: 0,
        });

        let message = '✅ Draft Day Refresh Complete!\n\n';

        // ADP results
        message += '📊 ADP:\n';
        for (const r of result.results.adp || []) {
            if (r.error) {
                message += `  • ${r.source}: ❌ ${r.error}\n`;
            } else {
                message += `  • ${r.source}: ✓ ${r.adp_updated} players\n`;
            }
        }

        // ECR (Expert Consensus Rankings)
        if (result.results.ecr) {
            if (result.results.ecr.error) {
                message += `\n🏆 ECR: ❌ ${result.results.ecr.error}\n`;
            } else {
                message += `\n🏆 ECR: ✓ ${result.results.ecr.updated} players (${result.results.ecr.experts} experts)\n`;
            }
        }

        // Injuries
        if (result.results.injuries) {
            if (result.results.injuries.error) {
                message += `\n🏥 Injuries: ❌ ${result.results.injuries.error}\n`;
            } else {
                message += `\n🏥 Injuries: ✓ ${result.results.injuries.updated} updated\n`;
            }
        }

        // Projections
        message += '\n📈 Projections:\n';
        for (const r of result.results.projections || []) {
            if (r.error) {
                message += `  • ${r.source}: ❌ ${r.error}\n`;
            } else {
                message += `  • ${r.source}: ✓ ${r.count} players\n`;
            }
        }

        // Rankings
        if (result.results.rankings) {
            message += `\n🏆 Rankings: ${result.results.rankings.error ? '❌' : '✓'} ${result.results.rankings.error || 'Refreshed'}\n`;
        }

        // News
        if (result.results.news) {
            message += `📰 News: ${result.results.news.error ? '❌' : '✓'} ${result.results.news.error || 'Refreshed'}\n`;
        }

        // Risk scores
        if (result.results.risk_scores) {
            message += `\n⚠️ Risk Scores: ✓ ${result.results.risk_scores.updated} recalculated\n`;
        }

        alert(message);

        // Reload UI data
        await loadPlayers();
        await loadRecommendations(); await loadScarcity();
        updateLastUpdated();

    } catch (error) {
        console.error('Failed to refresh all data:', error);
        alert('❌ Failed to refresh data. Check console for details.');
    } finally {
        btn.textContent = originalText;
        btn.disabled = false;
    }
}

// Sync ADP from ESPN and FantasyPros
async function syncADP() {
    try {
        const result = await fetchAPI('/players/sync-adp?source=all', { method: 'POST' });
        let message = 'ADP sync complete!\n\n';
        for (const r of result.results || []) {
            if (r.error) {
                message += `${r.source}: Error - ${r.error}\n`;
            } else {
                message += `${r.source}: Updated ${r.adp_updated} players\n`;
            }
        }
        alert(message);
        await loadPlayers();
        await loadRecommendations(); await loadScarcity();
    } catch (error) {
        console.error('Failed to sync ADP:', error);
        alert('Failed to sync ADP data.');
    }
}

// Sync injuries from ESPN
async function syncInjuries() {
    try {
        const result = await fetchAPI('/players/sync-injuries', { method: 'POST' });
        alert(`Injury sync complete!\n\nFound ${result.injured_from_espn} injured players on ESPN.\nUpdated: ${result.players_marked_injured}\nCleared: ${result.players_cleared}`);
        await loadPlayers();
        await loadRecommendations(); await loadScarcity();
    } catch (error) {
        console.error('Failed to sync injuries:', error);
        alert('Failed to sync injuries from ESPN. Check that ESPN credentials are configured.');
    }
}

// Toggle player injury status
async function toggleInjury(playerId, currentlyInjured) {
    const newStatus = !currentlyInjured;

    let injuryStatus = null;
    let injuryDetails = null;

    if (newStatus) {
        // Prompt for injury details
        injuryStatus = prompt('Injury status (IL-10, IL-60, DTD):', 'DTD');
        if (injuryStatus === null) return; // Cancelled
        injuryDetails = prompt('Injury details (optional):', '');
    }

    try {
        let url = `/players/${playerId}/injury?is_injured=${newStatus}`;
        if (injuryStatus) url += `&injury_status=${encodeURIComponent(injuryStatus)}`;
        if (injuryDetails) url += `&injury_details=${encodeURIComponent(injuryDetails)}`;

        await fetchAPI(url, { method: 'POST' });

        // Refresh player detail modal
        await showPlayerDetail(playerId);
        await loadPlayers();
    } catch (error) {
        console.error('Failed to update injury status:', error);
        alert('Failed to update injury status');
    }
}

// Toggle the Market Context drawer in the Decision Funnel sidebar
function toggleMarketContext() {
    const body = document.getElementById('market-context-body');
    const btn = document.getElementById('market-context-btn');
    const chevron = btn ? btn.querySelector('.market-context-chevron') : null;

    if (!body) return;

    const isCollapsed = body.classList.contains('collapsed');
    if (isCollapsed) {
        body.classList.remove('collapsed');
        if (chevron) chevron.style.transform = 'rotate(180deg)';
        if (btn) btn.setAttribute('aria-expanded', 'true');
    } else {
        body.classList.add('collapsed');
        if (chevron) chevron.style.transform = 'rotate(0deg)';
        if (btn) btn.setAttribute('aria-expanded', 'false');
    }
}

// Toggle collapsible section
function toggleSection(sectionName) {
    console.log('Toggling section:', sectionName);
    const section = document.querySelector(`.collapsible-section[data-section="${sectionName}"]`);
    if (!section) {
        console.log('Section not found:', sectionName);
        return;
    }

    const content = section.querySelector('.section-content');
    const chevron = section.querySelector('.section-chevron');
    
    if (!content || !chevron) {
        console.log('Content or chevron not found for section:', sectionName);
        return;
    }

    // Get current state
    const isCollapsed = collapsedSections.has(sectionName);
    
    const toggleBtn = section.querySelector('button[aria-expanded]');
    if (isCollapsed) {
        // Expand
        console.log('Expanding section:', sectionName);
        collapsedSections.delete(sectionName);
        content.classList.remove('collapsed');
        chevron.style.transform = 'rotate(0deg)';
        if (toggleBtn) toggleBtn.setAttribute('aria-expanded', 'true');
    } else {
        // Collapse
        console.log('Collapsing section:', sectionName);
        collapsedSections.add(sectionName);
        content.classList.add('collapsed');
        chevron.style.transform = 'rotate(-90deg)';
        if (toggleBtn) toggleBtn.setAttribute('aria-expanded', 'false');
    }

    // Save preference to localStorage
    localStorage.setItem('collapsedSections', JSON.stringify([...collapsedSections]));
}

// Restore collapsed sections from localStorage
function restoreCollapsedSections() {
    const saved = localStorage.getItem('collapsedSections');
    if (saved) {
        try {
            collapsedSections = new Set(JSON.parse(saved));
            collapsedSections.forEach(sectionName => {
                const section = document.querySelector(`.collapsible-section[data-section="${sectionName}"]`);
                if (section) {
                    const content = section.querySelector('.section-content');
                    const chevron = section.querySelector('.section-chevron');
                    const toggleBtn = section.querySelector('button[aria-expanded]');
                    if (content) content.classList.add('collapsed');
                    if (chevron) chevron.style.transform = 'rotate(-90deg)';
                    if (toggleBtn) toggleBtn.setAttribute('aria-expanded', 'false');
                }
            });
        } catch (e) {
            console.error('Failed to restore collapsed sections:', e);
        }
    }
}

// ==================== DRAFT MODE ====================

// Toggle draft mode on/off
function toggleDraftMode() {
    if (draftModeActive) {
        stopDraftMode();
    } else {
        startDraftMode();
    }
}

// Start draft mode - auto-sync with ESPN
async function startDraftMode() {
    // First, check if ESPN credentials are configured
    try {
        const result = await fetchAPI(`/draft/${currentLeagueId}/auto-sync`, { method: 'POST' });

        // If successful, start polling
        draftModeActive = true;
        document.getElementById('draft-mode-btn').classList.add('hidden');
        document.getElementById('draft-mode-indicator').classList.remove('hidden');
        document.getElementById('draft-mode-indicator').classList.add('flex');

        // Start polling interval
        draftModeInterval = setInterval(syncWithESPN, DRAFT_SYNC_INTERVAL);

        console.log('Draft mode started - syncing every 5 seconds');

        // Handle initial sync results
        if (result.newly_drafted_count > 0) {
            await loadPlayers();
            await loadRecommendations(); await loadScarcity();
        }

    } catch (error) {
        console.error('Failed to start draft mode:', error);

        // Show helpful error message
        if (error.message.includes('credentials')) {
            alert('ESPN credentials not configured.\n\nTo enable auto-sync:\n1. Get your espn_s2 and SWID cookies from ESPN\n2. Update your league settings with these values\n\nFor now, use the "Other" button to manually mark picks.');
        } else {
            alert('Failed to connect to ESPN. Check your credentials and try again.\n\nError: ' + error.message);
        }
    }
}

// Stop draft mode
function stopDraftMode() {
    draftModeActive = false;

    if (draftModeInterval) {
        clearInterval(draftModeInterval);
        draftModeInterval = null;
    }

    document.getElementById('draft-mode-btn').classList.remove('hidden');
    document.getElementById('draft-mode-indicator').classList.add('hidden');
    document.getElementById('draft-mode-indicator').classList.remove('flex');

    console.log('Draft mode stopped');
}

// Sync with ESPN (called by interval)
async function syncWithESPN() {
    if (!draftModeActive || !currentLeagueId) return;

    try {
        const result = await fetchAPI(`/draft/${currentLeagueId}/auto-sync`, { method: 'POST' });

        // If new players were drafted, refresh the list
        if (result.newly_drafted_count > 0) {
            console.log(`${result.newly_drafted_count} new picks detected:`, result.newly_drafted);
            await loadPlayers();
            await loadRecommendations(); await loadScarcity();
            updateLastUpdated();

            // Flash notification
            showDraftNotification(result.newly_drafted);
        }

    } catch (error) {
        console.error('ESPN sync failed:', error);
        // Don't stop draft mode on single failure, just log it
    }
}

// Show notification when players are drafted
function showDraftNotification(newPicks) {
    // Simple notification - could be enhanced with toast UI
    const names = newPicks.map(p => p.player_name).join(', ');
    console.log(`Drafted: ${names}`);

    // Update the pick info
    if (newPicks.length > 0) {
        const lastPick = newPicks[newPicks.length - 1];
        document.getElementById('current-pick-info').textContent =
            `Pick ${lastPick.pick_num + 1} - Round ${Math.ceil((lastPick.pick_num + 1) / 12)}`;
    }
}

// ==================== PROSPECT RADAR CHART ====================

// Store chart instance for cleanup
let prospectRadarChart = null;

/**
 * Render a radar chart for prospect scouting grades
 * @param {string} canvasId - The canvas element ID
 * @param {object} grades - Scouting grades object with hit, power, speed, arm, field
 */
function renderProspectRadarChart(canvasId, grades) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    // Destroy existing chart if present
    if (prospectRadarChart) {
        prospectRadarChart.destroy();
        prospectRadarChart = null;
    }

    // Extract grades (default to 50 if missing)
    const data = [
        grades.hit || 50,
        grades.power || 50,
        grades.speed || 50,
        grades.arm || 50,
        grades.field || 50
    ];

    const ctx = canvas.getContext('2d');
    prospectRadarChart = new Chart(ctx, {
        type: 'radar',
        data: {
            labels: ['Hit', 'Power', 'Speed', 'Arm', 'Field'],
            datasets: [{
                label: 'Scouting Grades',
                data: data,
                backgroundColor: 'rgba(236, 72, 153, 0.2)',
                borderColor: 'rgba(236, 72, 153, 1)',
                borderWidth: 2,
                pointBackgroundColor: 'rgba(236, 72, 153, 1)',
                pointBorderColor: '#fff',
                pointHoverBackgroundColor: '#fff',
                pointHoverBorderColor: 'rgba(236, 72, 153, 1)',
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            scales: {
                r: {
                    min: 20,
                    max: 80,
                    stepSize: 10,
                    ticks: {
                        display: false
                    },
                    grid: {
                        color: 'rgba(75, 85, 99, 0.5)'
                    },
                    angleLines: {
                        color: 'rgba(75, 85, 99, 0.5)'
                    },
                    pointLabels: {
                        color: 'rgba(209, 213, 219, 1)',
                        font: {
                            size: 11
                        }
                    }
                }
            },
            plugins: {
                legend: {
                    display: false
                },
                tooltip: {
                    callbacks: {
                        label: function(context) {
                            return context.raw + ' grade';
                        }
                    }
                }
            }
        }
    });
}

/**
 * Get CSS class for a scouting grade value
 * @param {number} grade - Grade value (20-80)
 * @returns {string} CSS class name
 */
function getGradeClass(grade) {
    if (!grade) return 'average';
    if (grade >= 60) return 'elite';
    if (grade >= 50) return 'above';
    if (grade >= 45) return 'average';
    return 'below';
}

/**
 * Render scouting grades grid for modal
 * @param {object} grades - Scouting grades object
 * @returns {string} HTML string
 */
function renderScoutingGradesGrid(grades) {
    if (!grades) return '<p class="text-gray-500">No scouting grades available</p>';

    const gradeLabels = [
        { key: 'hit', label: 'Hit' },
        { key: 'power', label: 'Power' },
        { key: 'speed', label: 'Speed' },
        { key: 'arm', label: 'Arm' },
        { key: 'field', label: 'Field' }
    ];

    let html = gradeLabels.map(g => {
        const value = grades[g.key];
        const gradeClass = getGradeClass(value);
        return `
            <div class="grade-item">
                <span class="grade-label">${g.label}</span>
                <span class="grade-value ${gradeClass}">${value || '--'}</span>
            </div>
        `;
    }).join('');

    // Add FV badge if available
    if (grades.fv) {
        html += `
            <div class="col-span-3 mt-2">
                <span class="fv-badge">FV ${grades.fv}</span>
            </div>
        `;
    }

    return html;
}

/**
 * Create FV star rating display with tooltip
 * @param {number} fv - Future Value rating (20-80 scale)
 * @returns {string} HTML string for star rating
 */
function createFVStarRating(fv) {
    if (!fv) return '';
    
    // Convert FV to star rating (5 stars total)
    // 20-30 = 1 star, 30-40 = 2 stars, 40-50 = 3 stars, 50-60 = 4 stars, 60+ = 5 stars
    let filledStars = 0;
    if (fv >= 65) filledStars = 5;
    else if (fv >= 55) filledStars = 4;
    else if (fv >= 45) filledStars = 3;
    else if (fv >= 35) filledStars = 2;
    else if (fv >= 20) filledStars = 1;
    
    // Determine color class and label based on FV value
    let starClass = '';
    let fvLabel = '';
    if (fv >= 70) {
        starClass = 'legendary';
        fvLabel = 'Elite';
    } else if (fv >= 60) {
        starClass = 'epic';
        fvLabel = 'Excellent';
    } else if (fv >= 50) {
        starClass = 'rare';
        fvLabel = 'Above Average';
    } else if (fv >= 40) {
        starClass = 'uncommon';
        fvLabel = 'Average';
    } else {
        starClass = 'common';
        fvLabel = 'Below Average';
    }
    
    // Create stars
    let starsHtml = '';
    for (let i = 1; i <= 5; i++) {
        const isFilled = i <= filledStars;
        const className = `fv-star ${isFilled ? 'filled ' + starClass : ''}`;
        starsHtml += `<span class="${className}">★</span>`;
    }
    
    // Create tooltip content
    const tooltipHtml = `
        <div class="fv-tooltip">
            <div class="fv-tooltip-header">FUTURE VALUE</div>
            <div class="fv-tooltip-grade ${starClass}">${fv}</div>
            <div>${fvLabel}</div>
        </div>
    `;
    
    return `
        <div class="fv-star-rating" data-fv="${fv}">
            ${starsHtml}
            ${tooltipHtml}
        </div>
    `;
}

/**
 * Render consensus rankings for modal
 * @param {object} consensus - Consensus data from API
 * @returns {string} HTML string
 */
function renderConsensusRankings(consensus) {
    if (!consensus) return '';

    let html = `
        <div class="consensus-main">
            <span class="consensus-rank">#${consensus.consensus_rank}</span>
            <span class="text-gray-400 ml-2">Consensus</span>
        </div>
    `;

    // Variance info
    if (consensus.variance) {
        html += `
            <div class="consensus-variance">
                <span class="text-gray-400">Variance:</span>
                <span class="ml-1 ${consensus.variance > 10 ? 'text-yellow-400' : 'text-gray-300'}">${consensus.variance.toFixed(1)}</span>
            </div>
        `;
    }

    // Opportunity score
    if (consensus.opportunity_score && consensus.opportunity_score > 20) {
        html += `
            <div class="opportunity-badge">
                Buying Opportunity: ${consensus.opportunity_score.toFixed(0)}%
            </div>
        `;
    }

    // Source breakdown
    if (consensus.sources && consensus.sources.length > 0) {
        html += `
            <div class="consensus-sources mt-2">
                ${consensus.sources.map(s => `
                    <span class="source-mini">${s.source}: #${s.rank || '--'}</span>
                `).join('')}
            </div>
        `;
    }

    return html;
}

/**
 * Render enhanced prospect card with scouting grades and consensus
 * @param {object} pick - Prospect pick data from API
 * @param {number} index - Index for animation delay
 * @returns {string} HTML string
 */
function renderEnhancedProspectCard(pick, index) {
    const hasGrades = pick.scouting_grades && (pick.scouting_grades.hit || pick.scouting_grades.power);
    const hasConsensus = pick.consensus && pick.consensus.consensus_rank;
    const hasOrgContext = pick.org_context && pick.org_context.current_level;

    return `
        <div class="recommendation-card prospect enhanced animate-slide-up" style="animation-delay: ${index * 75}ms">
            <div class="flex justify-between items-start mb-2">
                <div>
                    <button onclick="showPlayerDetail(${pick.player.id})" class="font-semibold hover:text-purple-400 transition-colors">
                        ${pick.player.name}
                    </button>
                    <div class="flex items-center gap-2 mt-1">
                        <span class="text-xs text-gray-400">${pick.player.team || 'MiLB'}</span>
                        ${renderPositionBadge(pick.player.positions)}
                        ${pick.eta ? `<span class="eta-badge">ETA ${pick.eta}</span>` : ''}
                    </div>
                </div>
                <div class="flex flex-col items-end gap-1">
                    ${pick.prospect_rank ? `<span class="prospect-rank-badge">#${pick.prospect_rank}</span>` : ''}
                    <span class="keeper-value-badge ${pick.keeper_value}">${pick.keeper_value}</span>
                    ${pick.keeper_value_score ? `<span class="text-xs text-gray-500">${pick.keeper_value_score.toFixed(0)} pts</span>` : ''}
                </div>
            </div>

            ${hasGrades ? `
                <div class="scouting-grades-mini mb-2">
                    <div class="grades-row">
                        ${['hit', 'power', 'speed', 'arm', 'field'].map(tool => {
                            const grade = pick.scouting_grades[tool];
                            const gradeClass = getGradeClass(grade);
                            return `<span class="grade-mini ${gradeClass}" title="${tool}">${tool[0].toUpperCase()}:${grade || '--'}</span>`;
                        }).join('')}
                        ${pick.scouting_grades.fv ? `<span class="fv-badge-mini">FV${pick.scouting_grades.fv}</span>` : ''}
                    </div>
                </div>
            ` : ''}

            ${hasOrgContext ? `
                <div class="org-context mb-2">
                    <span class="text-xs text-gray-400">
                        ${pick.org_context.current_level || ''}
                        ${pick.org_context.age ? `| Age ${pick.org_context.age}` : ''}
                        ${pick.org_context.org_rank ? `| #${pick.org_context.org_rank} in org` : ''}
                    </span>
                </div>
            ` : ''}

            ${hasConsensus && pick.consensus.sources.length > 1 ? `
                <div class="consensus-mini mb-2">
                    <div class="flex items-center gap-2 flex-wrap">
                        ${pick.consensus.sources.map(s => `
                            <span class="source-mini">${s.source}: #${s.rank || '--'}</span>
                        `).join('')}
                    </div>
                    ${pick.consensus.opportunity_score > 20 ? `
                        <span class="opportunity-mini">Opportunity: ${pick.consensus.opportunity_score.toFixed(0)}%</span>
                    ` : ''}
                </div>
            ` : ''}

            <p class="text-sm text-purple-300 mb-2 leading-relaxed">${pick.upside}</p>

            ${pick.position_scarcity_boost && pick.position_scarcity_boost !== 1.0 ? `
                <div class="position-boost mb-2">
                    <span class="text-xs text-purple-400">Position boost: ${pick.position_scarcity_boost.toFixed(2)}x</span>
                </div>
            ` : ''}

            ${pick.risk_factors && pick.risk_factors.length > 0 ? `
                <div class="flex flex-wrap gap-1">
                    ${pick.risk_factors.slice(0, 3).map(f => `
                        <span class="prospect-risk-tag">${f}</span>
                    `).join('')}
                </div>
            ` : ''}
            
            ${pick.scouting_grades && pick.scouting_grades.fv ? `
                <div class="mt-2">
                    ${createFVStarRating(pick.scouting_grades.fv)}
                </div>
            ` : ''}
        </div>
    `;
}

// Override the prospect rendering in renderRecommendations to use enhanced cards
const originalRenderRecommendations = renderRecommendations;
renderRecommendations = function() {
    // Call original function for most sections
    originalRenderRecommendations();

    // Re-render prospects with enhanced cards if they have scouting data
    const prospectsContainer = document.getElementById('prospect-picks');
    const prospects = recommendations.prospects || [];

    if (prospects.length > 0 && prospects.some(p => p.scouting_grades || p.consensus)) {
        prospectsContainer.innerHTML = prospects.map((pick, index) =>
            renderEnhancedProspectCard(pick, index)
        ).join('');
    }
};

// Update showPlayerDetail to handle prospect scouting grades
const originalShowPlayerDetail = showPlayerDetail;
showPlayerDetail = async function(playerId) {
    // Call original function
    await originalShowPlayerDetail(playerId);

    // Check if this player has prospect data in recommendations
    const prospectData = (recommendations.prospects || []).find(p => p.player.id === playerId);

    // Handle scouting grades display
    const scoutingSection = document.getElementById('modal-scouting');
    const gradesGrid = document.getElementById('modal-grades-grid');

    if (scoutingSection) {
        if (prospectData && prospectData.scouting_grades) {
            scoutingSection.classList.remove('hidden');
            if (gradesGrid) gradesGrid.innerHTML = renderScoutingGradesGrid(prospectData.scouting_grades);
            renderProspectRadarChart('modal-radar-chart', prospectData.scouting_grades);
        } else {
            scoutingSection.classList.add('hidden');
            if (prospectRadarChart) {
                prospectRadarChart.destroy();
                prospectRadarChart = null;
            }
        }
    }

    // Handle consensus display
    const consensusSection = document.getElementById('modal-consensus');
    const consensusContent = document.getElementById('modal-consensus-content');

    if (consensusSection) {
        if (prospectData && prospectData.consensus) {
            consensusSection.classList.remove('hidden');
            if (consensusContent) consensusContent.innerHTML = renderConsensusRankings(prospectData.consensus);
        } else {
            consensusSection.classList.add('hidden');
        }
    }
};

// ==================== MOCK DRAFT MODE ====================

// Show mock draft setup modal
async function showMockDraftSetup() {
    const modal = document.getElementById('mock-draft-modal');
    modal.classList.remove('hidden');

    // Load current draft status
    await updateDraftStatusDisplay();
}

// Close mock draft modal
function closeMockDraftModal() {
    document.getElementById('mock-draft-modal').classList.add('hidden');
}

// Update the draft status display in the modal
async function updateDraftStatusDisplay() {
    try {
        const response = await fetchAPI('/players/?available_only=false&limit=1');
        // Get count from a separate call
        const myTeam = await fetchAPI(withUserKey(`/players/my-team/roster?league_id=${currentLeagueId}`));

        // Count total drafted
        const allPlayers = await fetchAPI('/players/?limit=500&available_only=false');
        const draftedCount = allPlayers.filter(p => p.is_drafted).length;

        const statusDisplay = document.getElementById('draft-status-display');
        const countDisplay = document.getElementById('drafted-count');

        if (draftedCount > 0) {
            statusDisplay.classList.remove('hidden');
            countDisplay.textContent = `${draftedCount} players drafted (${myTeam.length} on my team)`;
        } else {
            statusDisplay.classList.add('hidden');
        }
    } catch (error) {
        console.log('Could not fetch draft status:', error);
    }
}

// Start quick practice mode (no ESPN connection)
async function startQuickPractice() {
    // Ask if they want to reset
    const draftedResponse = await fetchAPI('/players/?limit=500&available_only=false');
    const draftedCount = draftedResponse.filter(p => p.is_drafted).length;

    if (draftedCount > 0) {
        const reset = confirm(`${draftedCount} players are currently marked as drafted.\n\nWould you like to reset and start fresh?\n\nClick OK to reset, or Cancel to continue with current state.`);
        if (reset) {
            await resetDraft();
        }
    }

    // Close modal and show draft mode button
    closeMockDraftModal();
    document.getElementById('draft-mode-btn').classList.remove('hidden');
    document.getElementById('mock-draft-btn').textContent = 'Mock Draft Active';
    document.getElementById('mock-draft-btn').classList.remove('from-amber-500', 'to-orange-500');
    document.getElementById('mock-draft-btn').classList.add('from-emerald-500', 'to-emerald-600');

    // Refresh the player list
    await loadPlayers();
    await loadRecommendations(); await loadScarcity();

    alert('Mock Draft Practice Started!\n\nUse the "Mine" button to track your picks and "Other" for opponent picks.\n\nYour recommendations will update automatically as players are drafted.');
}

// Connect to ESPN mock draft
async function connectESPNMock() {
    const leagueIdInput = document.getElementById('mock-league-id');
    const leagueId = leagueIdInput.value.trim();

    if (!leagueId) {
        alert('Please enter the ESPN Mock Draft League ID');
        return;
    }

    try {
        // Create a temporary league connection
        const response = await fetch('/api/v1/leagues/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                espn_league_id: parseInt(leagueId),
                year: 2026,
                name: `Mock Draft ${leagueId}`
            })
        });

        if (!response.ok) {
            const error = await response.json();
            if (error.detail && error.detail.includes('already connected')) {
                // League already exists, that's fine
                console.log('League already connected');
            } else {
                throw new Error(error.detail || 'Failed to create league');
            }
        }

        // Get the league we just created or find existing
        const leagues = await fetchAPI('/leagues/');
        const mockLeague = leagues.find(l => l.espn_league_id === parseInt(leagueId));

        if (mockLeague) {
            currentLeagueId = mockLeague.id;

            // Show draft mode controls
            closeMockDraftModal();
            document.getElementById('draft-mode-btn').classList.remove('hidden');
            document.getElementById('mock-draft-btn').textContent = 'ESPN Connected';
            document.getElementById('mock-draft-btn').classList.remove('from-amber-500', 'to-orange-500');
            document.getElementById('mock-draft-btn').classList.add('from-blue-500', 'to-blue-600');

            // Pre-fill session name and open draft session setup modal
            const sessionNameInput = document.getElementById('draft-session-name');
            if (sessionNameInput) {
                sessionNameInput.value = 'ESPN Mock Draft';
            }
            showDraftSessionModal();
        }

    } catch (error) {
        console.error('Failed to connect to ESPN:', error);
        alert(`Failed to connect: ${error.message}\n\nYou can still use Quick Practice Mode for manual tracking.`);
    }
}

// Reset all draft state
async function resetDraft() {
    // Check if session is active (frontend guard)
    if (draftSessionId) {
        alert(`Cannot reset draft while session "${draftSessionName}" is active.\n\nEnd the session first to reset.`);
        return;
    }

    try {
        const result = await fetchAPI('/players/reset-draft?confirm=true', { method: 'POST' });
        console.log('Draft reset:', result);

        // Refresh the UI
        await loadPlayers();
        await loadRecommendations(); await loadScarcity();
        await loadMyTeam();
        await updateDraftStatusDisplay();

        alert(`Draft Reset Complete!\n\n${result.players_reset} players marked as undrafted.\n\nReady for a fresh mock draft!`);

    } catch (error) {
        console.error('Failed to reset draft:', error);
        // Check if it's a session-related error
        if (error.message && error.message.includes('session')) {
            alert(error.message);
        } else {
            alert('Failed to reset draft state.');
        }
    }
}

// Close modal when clicking outside
document.addEventListener('click', function(event) {
    const modal = document.getElementById('mock-draft-modal');
    if (event.target === modal) {
        closeMockDraftModal();
    }
    const sessionModal = document.getElementById('draft-session-modal');
    if (event.target === sessionModal) {
        closeDraftSessionModal();
    }
    const historyModal = document.getElementById('session-history-modal');
    if (event.target === historyModal) {
        closeSessionHistoryModal();
    }
});

// ==================== DRAFT SESSION MODE ====================

// Show draft session entry modal
function showDraftSessionModal() {
    const modal = document.getElementById('draft-session-modal');
    modal.classList.remove('hidden');

    // Pre-populate from saved config if available
    const saved = loadDraftConfig();
    if (saved) {
        document.getElementById('draft-num-teams').value = saved.numTeams || 12;
        window._savedDraftTeamNames = saved.teamNames || [];
        window._savedDraftUserPosition = saved.userPosition || 1;
    } else {
        window._savedDraftTeamNames = [];
        window._savedDraftUserPosition = null;
    }

    // Initialize team count selector
    const numTeamsSelect = document.getElementById('draft-num-teams');
    numTeamsSelect.addEventListener('change', updateDraftPositionOptions);
    updateDraftPositionOptions();

    // Restore user position after options are populated
    if (saved?.userPosition) {
        document.getElementById('draft-user-position').value = saved.userPosition;
    }

    document.getElementById('draft-session-name').focus();
}

// Update draft position options based on number of teams
function updateDraftPositionOptions() {
    const numTeams = parseInt(document.getElementById('draft-num-teams').value);
    const positionSelect = document.getElementById('draft-user-position');
    positionSelect.innerHTML = '';

    for (let i = 1; i <= numTeams; i++) {
        const option = document.createElement('option');
        option.value = i;
        option.textContent = `Pick ${i}`;
        positionSelect.appendChild(option);
    }

    // Update team names container
    updateTeamNamesContainer(numTeams);
}

// Update team names input container
function updateTeamNamesContainer(numTeams) {
    const container = document.getElementById('team-names-container');
    container.innerHTML = '';

    for (let i = 1; i <= numTeams; i++) {
        const div = document.createElement('div');
        div.className = 'flex items-center gap-2';
        // Pre-fill from keeper team names, then saved config, then empty
        const prefill = keeperTeamNames[i - 1]
            || (window._savedDraftTeamNames && window._savedDraftTeamNames[i - 1])
            || '';
        div.innerHTML = `
            <span class="text-xs text-gray-500 w-6">${i}.</span>
            <input type="text" id="team-name-${i}" placeholder="Team ${i}" value="${escapeHtml(prefill)}"
                   class="flex-1 px-2 py-1 bg-gray-800 border border-gray-700 rounded text-sm focus:border-amber-500 focus:outline-none">
        `;
        container.appendChild(div);
    }
}

// Update team name input fields in the Teams tab
function updateTeamsTabNameFields() {
    const numTeams = parseInt(document.getElementById('teams-num-teams').value);
    const container = document.getElementById('teams-names-container');
    // Snapshot existing values before rebuilding
    const existing = {};
    for (let i = 1; i <= 16; i++) {
        const el = document.getElementById(`teams-name-${i}`);
        if (el) existing[i] = el.value;
    }
    container.innerHTML = '';
    for (let i = 1; i <= numTeams; i++) {
        const div = document.createElement('div');
        div.className = 'flex items-center gap-2';
        const prefill = existing[i] || (window._savedDraftTeamNames && window._savedDraftTeamNames[i - 1]) || '';
        div.innerHTML = `
            <span class="text-xs text-gray-500 w-6">${i}.</span>
            <input type="text" id="teams-name-${i}" placeholder="Team ${i}" value="${escapeHtml(prefill)}"
                   class="flex-1 px-2 py-1 bg-gray-800 border border-gray-700 rounded text-sm focus:border-amber-500 focus:outline-none">
        `;
        container.appendChild(div);
    }
}

// Save team order config from the Teams tab to localStorage
async function saveTeamsTabConfig() {
    if (!currentLeagueId) return;
    const numTeams = parseInt(document.getElementById('teams-num-teams').value);
    const teamNames = [];
    for (let i = 1; i <= numTeams; i++) {
        teamNames.push(document.getElementById(`teams-name-${i}`)?.value.trim() || '');
    }
    // Preserve userPosition from any previously saved config
    const existing = loadDraftConfig();
    const userPosition = existing?.userPosition || 1;
    localStorage.setItem(DRAFT_CONFIG_KEY(currentLeagueId), JSON.stringify({ numTeams, userPosition, teamNames }));
    window._savedDraftTeamNames = teamNames;
    updateKeeperTeamSelector();
    try {
        await fetchAPI(`/leagues/${currentLeagueId}/teams/manual`, {
            method: 'POST',
            body: JSON.stringify({ num_teams: numTeams, team_names: teamNames }),
        });
        await loadClaimableTeams();
        showNotification('Draft order saved', 'success');
    } catch (error) {
        console.error('Failed to sync teams for claiming:', error);
        showNotification('Saved locally, but failed to sync teams to server', 'error');
    }
}

// Load saved config into the Teams tab UI
function loadTeamsTabConfig() {
    const saved = loadDraftConfig();
    if (saved) {
        document.getElementById('teams-num-teams').value = saved.numTeams || 12;
        window._savedDraftTeamNames = saved.teamNames || [];
    }
    updateTeamsTabNameFields();
    updateKeeperTeamSelector();
}

// Close draft session modal
function closeDraftSessionModal() {
    document.getElementById('draft-session-modal').classList.add('hidden');
    document.getElementById('draft-session-name').value = '';
}

// Save draft config (team count, user position, team names) to localStorage
function saveDraftConfig() {
    if (!currentLeagueId) return;
    const numTeams = parseInt(document.getElementById('draft-num-teams').value);
    const userPosition = parseInt(document.getElementById('draft-user-position').value);
    const teamNames = [];
    for (let i = 1; i <= numTeams; i++) {
        const val = document.getElementById(`team-name-${i}`)?.value.trim() || '';
        teamNames.push(val);
    }
    localStorage.setItem(DRAFT_CONFIG_KEY(currentLeagueId), JSON.stringify({ numTeams, userPosition, teamNames }));
    showNotification('Draft order saved', 'success');
}

// Load saved draft config from localStorage
function loadDraftConfig() {
    if (!currentLeagueId) return null;
    try {
        const raw = localStorage.getItem(DRAFT_CONFIG_KEY(currentLeagueId));
        return raw ? JSON.parse(raw) : null;
    } catch { return null; }
}

// Start a new draft session
async function startDraftSession() {
    const nameInput = document.getElementById('draft-session-name');
    const name = nameInput.value.trim();

    if (!name) {
        alert('Please enter a session name');
        nameInput.focus();
        return;
    }

    const numTeams = parseInt(document.getElementById('draft-num-teams').value);
    const userPosition = parseInt(document.getElementById('draft-user-position').value);

    // Collect team names
    const teamNames = [];
    for (let i = 1; i <= numTeams; i++) {
        const input = document.getElementById(`team-name-${i}`);
        teamNames.push(input?.value.trim() || `Team ${i}`);
    }

    try {
        const teamNamesParam = encodeURIComponent(JSON.stringify(teamNames));
        const response = await fetchAPI(
            `/draft/session/start?session_name=${encodeURIComponent(name)}&league_id=${currentLeagueId}&num_teams=${numTeams}&user_draft_position=${userPosition}&draft_type=snake&team_names=${teamNamesParam}`,
            { method: 'POST' }
        );

        draftSessionId = response.session_id;
        draftSessionName = name;
        draftNumTeams = numTeams;
        draftUserPosition = userPosition;
        draftCurrentPick = response.current_pick;
        draftCurrentRound = response.current_round;
        draftTeamOnClock = response.team_on_clock;
        draftIsUserPick = response.is_user_pick;
        canUndo = false;
        canRedo = false;

        // Build teams array with custom names
        draftTeams = [];
        for (let i = 1; i <= numTeams; i++) {
            draftTeams.push({
                id: null,  // We may not have real team IDs
                name: teamNames[i - 1],
                draft_position: i,
                is_user_team: i === userPosition
            });
        }
        updateKeeperTeamSelector();

        // Persist config for next time
        saveDraftConfig();

        // Reset achievement state for new session
        resetAchievementState();

        closeDraftSessionModal();
        showDraftSessionBar();
        updateOnTheClockDisplay();
        updateResetButtonState();

        // If connected to an ESPN league, start ESPN pick auto-sync
        if (currentLeagueId) {
            try {
                await startDraftMode();
            } catch (e) {
                console.log('ESPN auto-sync not available, using manual mode');
            }
        }

        console.log('Draft session started:', draftSessionName, 'Teams:', draftTeams.length);

    } catch (error) {
        console.error('Failed to start draft session:', error);
        alert('Failed to start draft session: ' + (error.message || 'Unknown error'));
    }
}

// Check for active session on page load
async function checkActiveSession() {
    if (!currentLeagueId) return;

    try {
        const response = await fetchAPI(`/draft/session/active?league_id=${currentLeagueId}`);

        if (response.is_active) {
            draftSessionId = response.session_id;
            draftSessionName = response.session_name;
            draftNumTeams = response.num_teams;
            draftUserPosition = response.user_draft_position;
            draftCurrentPick = response.current_pick;
            draftCurrentRound = response.current_round;
            draftTeamOnClock = response.team_on_clock;
            draftIsUserPick = response.is_user_pick;
            draftTeams = response.teams || [];
            updateKeeperTeamSelector();
            canUndo = response.can_undo;
            canRedo = response.can_redo;

            // If no teams from API, create default ones
            if (draftTeams.length === 0) {
                for (let i = 1; i <= draftNumTeams; i++) {
                    draftTeams.push({
                        id: null,
                        name: `Team ${i}`,
                        draft_position: i,
                        is_user_team: i === draftUserPosition
                    });
                }
            }

            showDraftSessionBar();
            updateOnTheClockDisplay();
            updateUndoRedoButtons();
            updateResetButtonState();

            console.log('Restored active draft session:', draftSessionName);
        }
    } catch (error) {
        console.error('Failed to check active session:', error);
    }
}

// Update session state (pick count, undo/redo availability)
async function updateSessionState() {
    if (!draftSessionId || !currentLeagueId) return;

    try {
        const response = await fetchAPI(`/draft/session/active?league_id=${currentLeagueId}`);

        if (response.is_active) {
            draftCurrentPick = response.current_pick;
            draftCurrentRound = response.current_round;
            draftTeamOnClock = response.team_on_clock;
            draftIsUserPick = response.is_user_pick;
            canUndo = response.can_undo;
            canRedo = response.can_redo;

            updateOnTheClockDisplay();
            updateUndoRedoButtons();

            // Refresh history and teams tabs if they're currently visible
            const historyTab = document.getElementById('tab-draft-history');
            if (historyTab && !historyTab.classList.contains('hidden')) {
                loadDraftHistory();
            }
            const teamsTab = document.getElementById('tab-teams');
            if (teamsTab && !teamsTab.classList.contains('hidden')) {
                loadSelectedTeamRoster();
            }
        }
    } catch (error) {
        console.error('Failed to update session state:', error);
    }
}

// Show the draft session bar
function showDraftSessionBar() {
    const bar = document.getElementById('draft-session-bar');
    bar.classList.remove('hidden');

    document.getElementById('session-name-display').textContent = draftSessionName;
    updateOnTheClockDisplay();
    updateUndoRedoButtons();
}

// Hide the draft session bar
function hideDraftSessionBar() {
    document.getElementById('draft-session-bar').classList.add('hidden');
}

// Update the "On The Clock" display
function updateOnTheClockDisplay() {
    const pickDisplay = document.getElementById('current-pick-display');
    const roundDisplay = document.getElementById('current-round-display');
    const teamDisplay = document.getElementById('on-the-clock-team');
    const userIndicator = document.getElementById('user-pick-indicator');
    const xpText = document.getElementById('xp-text');
    const xpFill = document.getElementById('xp-bar-fill');

    if (pickDisplay) pickDisplay.textContent = draftCurrentPick;
    if (roundDisplay) roundDisplay.textContent = draftCurrentRound;

    // Get team name for current pick
    const teamOnClock = draftTeams.find(t => t.draft_position === draftTeamOnClock);
    const teamName = teamOnClock ? teamOnClock.name : `Team ${draftTeamOnClock}`;

    if (teamDisplay) {
        teamDisplay.textContent = teamName;
        teamDisplay.className = draftIsUserPick
            ? 'font-bold text-lg text-green-400'
            : 'font-bold text-lg text-cyan-400';
    }

    if (userIndicator) {
        userIndicator.classList.toggle('hidden', !draftIsUserPick);
    }

    // Update XP bar
    const totalPicks = draftNumTeams * 23;  // Assuming 23 rounds
    const progress = Math.min((draftCurrentPick / totalPicks) * 100, 100);
    if (xpFill) xpFill.style.width = `${progress}%`;
    if (xpText) xpText.textContent = `PICK ${draftCurrentPick} OF ${totalPicks}`;
}

// Update undo/redo button states
function updateUndoRedoButtons() {
    const undoBtn = document.getElementById('undo-btn');
    const redoBtn = document.getElementById('redo-btn');

    if (undoBtn) {
        undoBtn.disabled = !canUndo;
    }
    if (redoBtn) {
        redoBtn.disabled = !canRedo;
    }
}

// Update pick count display (legacy - kept for compatibility)
function updatePickCountDisplay(count) {
    // Now handled by updateOnTheClockDisplay
    const display = document.getElementById('session-pick-count');
    if (display) {
        display.textContent = count > 0 ? `(${count} pick${count !== 1 ? 's' : ''})` : '';
    }
}

// Undo last pick
async function undoPick() {
    if (!draftSessionId || !canUndo) return;

    try {
        const response = await fetchAPI(
            `/draft/session/undo?session_id=${draftSessionId}`,
            { method: 'POST' }
        );

        canUndo = response.can_undo;
        canRedo = response.can_redo;
        updateUndoRedoButtons();

        // Refresh player list and recommendations
        await loadPlayers();
        await loadRecommendations(); await loadScarcity();
        await updateSessionState();

        // Mark unsaved changes after undo action
        markUnsavedChanges();

        console.log(`Undone: ${response.player_name} (${response.action_undone})`);

    } catch (error) {
        console.error('Failed to undo:', error);
        alert('Failed to undo: ' + (error.message || 'Nothing to undo'));
    }
}

// Redo last undone pick
async function redoPick() {
    if (!draftSessionId || !canRedo) return;

    try {
        const response = await fetchAPI(
            `/draft/session/redo?session_id=${draftSessionId}`,
            { method: 'POST' }
        );

        canUndo = response.can_undo;
        canRedo = response.can_redo;
        updateUndoRedoButtons();

        // Refresh player list and recommendations
        await loadPlayers();
        await loadRecommendations(); await loadScarcity();
        await updateSessionState();

        // Mark unsaved changes after redo action
        markUnsavedChanges();

        console.log(`Redone: ${response.player_name} (${response.action_redone})`);

    } catch (error) {
        console.error('Failed to redo:', error);
        alert('Failed to redo: ' + (error.message || 'Nothing to redo'));
    }
}

// ==================== SESSION PERSISTENCE MODULE ====================

// Session persistence state
let sessionPersistenceEnabled = true;
let lastAutoSave = null;
let autoSaveInterval = null;
const AUTO_SAVE_INTERVAL_MS = 30000; // 30 seconds
let localSessionState = {};
let hasUnsavedChanges = false;

// Initialize session persistence
async function initSessionPersistence() {
    if (!sessionPersistenceEnabled) return;

    // Show session controls
    document.getElementById('save-session-btn').classList.remove('hidden');
    document.getElementById('restore-session-btn').classList.remove('hidden');
    document.getElementById('session-status').classList.remove('hidden');

    // Load any locally saved session data
    loadLocalSessionState();

    // Start auto-save interval
    startAutoSave();

    // Check for saved sessions for this league
    await loadSavedSessions();

    console.log('Session persistence initialized');
}

const STATE_VERSION = 1;

// Load locally saved session state from localStorage
function loadLocalSessionState() {
    try {
        const savedState = localStorage.getItem(`fbb_session_${currentLeagueId}`);
        if (!savedState) return;

        const parsed = JSON.parse(savedState);
        // Reject saved state from an incompatible schema version
        if (!parsed || parsed.version !== STATE_VERSION) {
            console.warn('Saved session schema mismatch — discarding stale state.');
            localStorage.removeItem(`fbb_session_${currentLeagueId}`);
            return;
        }

        localSessionState = parsed;
        console.log('Loaded local session state');
    } catch (error) {
        console.error('Failed to load local session state:', error);
    }
}

// Save current session state
async function saveCurrentSession() {
    if (!currentLeagueId) {
        console.warn('No current league, cannot save session');
        return;
    }

    try {
        // Collect current draft state
        const draftState = collectDraftState();

        // Save to server if we have an active session
        if (draftSessionId) {
            const response = await fetchAPI(
                `/draft/sessions/${draftSessionId}`,
                {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ draft_state: draftState })
                }
            );

            if (response.status === 'updated') {
                lastAutoSave = new Date();
                updateSessionStatusDisplay();
                console.log('Session saved to server');
            }
        }

        // Save to localStorage as backup (include schema version for future migrations)
        localStorage.setItem(
            `fbb_session_${currentLeagueId}`,
            JSON.stringify({
                version: STATE_VERSION,
                ...draftState,
                savedAt: new Date().toISOString(),
                leagueId: currentLeagueId
            })
        );

        hasUnsavedChanges = false;
        showNotification('Session saved successfully', 'success');

    } catch (error) {
        console.error('Failed to save session:', error);
        showNotification('Failed to save session: ' + error.message, 'error');
    }
}

// Auto-save session state periodically
function startAutoSave() {
    if (autoSaveInterval) {
        clearInterval(autoSaveInterval);
    }

    autoSaveInterval = setInterval(async () => {
        if (hasUnsavedChanges && draftSessionId) {
            await saveCurrentSession();
        }
    }, AUTO_SAVE_INTERVAL_MS);
}

// Collect current draft state for persistence
function collectDraftState() {
    return {
        // Current draft session info
        sessionId: draftSessionId,
        sessionName: draftSessionName,
        leagueId: currentLeagueId,

        // Draft state
        currentPick: draftCurrentPick,
        currentRound: draftCurrentRound,
        teamOnClock: draftTeamOnClock,
        isUserPick: draftIsUserPick,
        numTeams: draftNumTeams,
        userDraftPosition: draftUserPosition,
        teams: draftTeams,

        // Player data snapshot
        players: players.map(p => ({
            id: p.id,
            name: p.name,
            is_drafted: p.is_drafted,
            drafted_by_team_id: p.drafted_by_team_id
        })),

        // UI state
        selectedPositions: Array.from(selectedPositions),
        currentTeam: currentTeam,
        searchQuery: searchQuery,

        // Timestamp
        savedAt: new Date().toISOString()
    };
}

// Load saved sessions for current league
async function loadSavedSessions() {
    if (!currentLeagueId) return;

    try {
        const response = await fetchAPI(`/draft/sessions/league/${currentLeagueId}?include_inactive=true`);

        const sessionsList = document.getElementById('saved-sessions-list');
        if (response.sessions && response.sessions.length > 0) {
            sessionsList.innerHTML = response.sessions.map(session => `
                <div class="p-2 hover:bg-gray-700 rounded cursor-pointer flex justify-between items-center"
                     onclick="restoreSession('${session.session_id}')">
                    <div>
                        <div class="font-medium">${escapeHtml(session.session_name)}</div>
                        <div class="text-xs text-gray-400">
                            Saved: ${new Date(session.updated_at).toLocaleString()}
                        </div>
                    </div>
                    <button onclick="event.stopPropagation(); deleteSession('${session.session_id}')"
                            class="text-red-400 hover:text-red-300 p-1" title="Delete session">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path>
                        </svg>
                    </button>
                </div>
            `).join('');
        } else {
            sessionsList.innerHTML = '<div class="text-sm text-gray-500 text-center py-4">No saved sessions</div>';
        }

    } catch (error) {
        console.error('Failed to load saved sessions:', error);
    }
}

// Show restore session dropdown
function showRestoreSessionDropdown() {
    const dropdown = document.getElementById('restore-session-dropdown');
    dropdown.classList.toggle('hidden');

    // Close dropdown when clicking elsewhere
    if (!dropdown.classList.contains('hidden')) {
        setTimeout(() => {
            const closeHandler = (e) => {
                if (!document.getElementById('restore-session-btn').contains(e.target) &&
                    !dropdown.contains(e.target)) {
                    dropdown.classList.add('hidden');
                    document.removeEventListener('click', closeHandler);
                }
            };
            document.addEventListener('click', closeHandler);
        }, 100);
    }
}

// Restore a saved session
async function restoreSession(sessionId) {
    try {
        const response = await fetchAPI(`/draft/sessions/${sessionId}`);

        if (response.draft_state) {
            // Confirm restoration if there are unsaved changes
            if (hasUnsavedChanges) {
                const confirmed = confirm(
                    'You have unsaved changes that will be lost. Continue with session restoration?'
                );
                if (!confirmed) return;
            }

            // Apply the restored state
            applyDraftState(response.draft_state);

            // Hide dropdown
            document.getElementById('restore-session-dropdown').classList.add('hidden');

            showNotification('Session restored successfully', 'success');
        }

    } catch (error) {
        console.error('Failed to restore session:', error);
        showNotification('Failed to restore session: ' + error.message, 'error');
    }
}

// Apply restored draft state
function applyDraftState(state) {
    // Restore draft session info
    draftSessionId = state.sessionId;
    draftSessionName = state.sessionName;
    currentLeagueId = state.leagueId;

    // Restore draft state
    draftCurrentPick = state.currentPick;
    draftCurrentRound = state.currentRound;
    draftTeamOnClock = state.teamOnClock;
    draftIsUserPick = state.isUserPick;
    draftNumTeams = state.numTeams;
    draftUserPosition = state.userDraftPosition;
    draftTeams = state.teams || [];

    // Restore UI state
    selectedPositions = new Set(state.selectedPositions || []);
    currentTeam = state.currentTeam || '';
    searchQuery = state.searchQuery || '';

    // Update UI
    if (draftSessionId) {
        showDraftSessionBar();
    }

    // Refresh data displays
    renderPlayerList();
    updateOnTheClockDisplay();

    // Update last saved timestamp
    if (state.savedAt) {
        lastAutoSave = new Date(state.savedAt);
        updateSessionStatusDisplay();
    }

    hasUnsavedChanges = false;
}

// Delete a saved session
async function deleteSession(sessionId) {
    if (!confirm('Delete this saved session? This cannot be undone.')) {
        return;
    }

    try {
        const response = await fetchAPI(`/draft/sessions/${sessionId}`, {
            method: 'DELETE'
        });

        if (response.status === 'deleted') {
            showNotification('Session deleted', 'success');
            await loadSavedSessions(); // Refresh the list
        }

    } catch (error) {
        console.error('Failed to delete session:', error);
        showNotification('Failed to delete session: ' + error.message, 'error');
    }
}

// Update session status display
function updateSessionStatusDisplay() {
    const statusElement = document.getElementById('last-saved-timestamp');
    if (lastAutoSave) {
        statusElement.textContent = lastAutoSave.toLocaleTimeString();
    } else {
        statusElement.textContent = '--';
    }
}

// Mark that there are unsaved changes
function markUnsavedChanges() {
    hasUnsavedChanges = true;
}

// Show notification message
function showNotification(message, type = 'info') {
    // Simple notification - in a real implementation, you might want a more sophisticated toast system
    console.log(`[${type.toUpperCase()}] ${message}`);

    // For now, we'll just use a simple alert for errors
    if (type === 'error') {
        alert(message);
    }
}

// End draft session with confirmation
async function confirmEndSession() {
    if (!draftSessionId) return;

    const confirmed = confirm(
        `End draft session "${draftSessionName}"?\n\n` +
        `You will be able to reset the draft board after this.\n` +
        `Pick history will be preserved.`
    );

    if (!confirmed) return;

    try {
        const response = await fetchAPI(
            `/draft/session/end?session_id=${draftSessionId}`,
            { method: 'POST' }
        );

        const totalPicks = response.total_picks;

        // Clear session state
        draftSessionId = null;
        draftSessionName = null;
        canUndo = false;
        canRedo = false;

        hideDraftSessionBar();
        updateResetButtonState();

        // Stop ESPN auto-sync polling if it was running
        if (draftModeActive) {
            stopDraftMode();
        }

        alert(`Draft session ended!\n\n${totalPicks} picks recorded.`);

    } catch (error) {
        console.error('Failed to end session:', error);
        alert('Failed to end session: ' + (error.message || 'Unknown error'));
    }
}

// Show session history modal
async function showSessionHistory() {
    if (!draftSessionId) return;

    const modal = document.getElementById('session-history-modal');
    const content = document.getElementById('session-history-content');

    content.innerHTML = '<div class="text-center py-4 text-gray-400">Loading history...</div>';
    modal.classList.remove('hidden');

    try {
        const response = await fetchAPI(
            `/draft/session/history?session_id=${draftSessionId}&include_undone=true`
        );

        if (response.history.length === 0) {
            content.innerHTML = '<div class="text-center py-8 text-gray-500">No picks recorded yet</div>';
            return;
        }

        content.innerHTML = `
            <div class="space-y-2">
                ${response.history.map((pick, index) => {
                    const isUndone = pick.is_undone;
                    const actionLabel = pick.action === 'draft_mine' ? 'My Pick' :
                                       pick.action === 'draft_other' ? 'Other Team' : 'Undrafted';
                    const actionColor = pick.action === 'draft_mine' ? 'text-emerald-400' :
                                       pick.action === 'draft_other' ? 'text-gray-400' : 'text-red-400';

                    return `
                        <div class="flex items-center justify-between p-3 rounded-lg ${isUndone ? 'bg-gray-900/50 opacity-50' : 'bg-gray-700/50'}">
                            <div class="flex items-center gap-3">
                                <span class="text-sm font-mono text-gray-500">#${pick.sequence_num}</span>
                                <span class="${isUndone ? 'line-through text-gray-500' : 'font-medium'}">${pick.player_name}</span>
                            </div>
                            <div class="flex items-center gap-2">
                                <span class="text-xs ${actionColor}">${actionLabel}</span>
                                ${isUndone ? '<span class="text-xs text-yellow-500">(undone)</span>' : ''}
                            </div>
                        </div>
                    `;
                }).join('')}
            </div>
        `;

    } catch (error) {
        console.error('Failed to load history:', error);
        content.innerHTML = '<div class="text-center py-4 text-red-400">Failed to load history</div>';
    }
}

// Close session history modal
function closeSessionHistoryModal() {
    document.getElementById('session-history-modal').classList.add('hidden');
}

// Update reset button state based on session
function updateResetButtonState() {
    // Find the reset button in the mock draft modal
    const resetBtn = document.querySelector('button[onclick="resetDraft()"]');
    if (resetBtn) {
        if (draftSessionId) {
            resetBtn.disabled = true;
            resetBtn.title = 'End draft session first to reset';
            resetBtn.classList.add('opacity-50', 'cursor-not-allowed');
        } else {
            resetBtn.disabled = false;
            resetBtn.title = '';
            resetBtn.classList.remove('opacity-50', 'cursor-not-allowed');
        }
    }
}

// Setup keyboard shortcuts for undo/redo
function setupKeyboardShortcuts() {
    document.addEventListener('keydown', function(e) {
        // Escape closes any open modal
        if (e.key === 'Escape') {
            const playerModal = document.getElementById('player-modal');
            if (playerModal && !playerModal.classList.contains('hidden')) {
                closeModal();
                return;
            }
        }

        // Only handle session shortcuts if session is active
        if (!draftSessionId) return;

        // Ignore if typing in an input
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

        // Ctrl+Z or Cmd+Z for undo
        if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey) {
            e.preventDefault();
            undoPick();
        }

        // Ctrl+Y or Cmd+Shift+Z for redo
        if ((e.ctrlKey || e.metaKey) && (e.key === 'y' || (e.key === 'z' && e.shiftKey))) {
            e.preventDefault();
            redoPick();
        }
    });
}

// ==================== TEAM PICKER MODAL ====================

// Show team picker modal for selecting which team drafted a player
function showTeamPickerModal(playerId, buttonEl) {
    teamPickerPlayerId = playerId;
    const playerName = buttonEl.dataset.playerName || 'Unknown';

    const modal = document.getElementById('team-picker-modal');
    const playerNameEl = document.getElementById('team-picker-player-name');
    const content = document.getElementById('team-picker-content');

    playerNameEl.textContent = `Drafting: ${playerName}`;

    // Build team buttons (excluding user's team since they use "Mine")
    content.innerHTML = draftTeams
        .filter(team => team.draft_position !== draftUserPosition)
        .map(team => {
            const isOnClock = team.draft_position === draftTeamOnClock;
            const clockBadge = isOnClock ? '<span class="ml-2 text-yellow-400 text-xs">⏰ On Clock</span>' : '';
            return `
                <button onclick="selectTeamForPick(${team.draft_position})"
                        class="w-full p-3 text-left rounded-lg transition-all ${isOnClock ? 'bg-yellow-600/30 border border-yellow-500/50 hover:bg-yellow-600/50' : 'bg-gray-700/50 hover:bg-gray-600/50'}">
                    <span class="font-medium">${team.name}</span>
                    <span class="text-gray-400 text-sm ml-2">(Pick ${team.draft_position})</span>
                    ${clockBadge}
                </button>
            `;
        }).join('');

    modal.classList.remove('hidden');
}

// Close team picker modal
function closeTeamPickerModal() {
    document.getElementById('team-picker-modal').classList.add('hidden');
    teamPickerPlayerId = null;
}

// Select a team for the current pick
async function selectTeamForPick(teamDraftPosition) {
    if (!teamPickerPlayerId) return;

    const playerId = teamPickerPlayerId;
    closeTeamPickerModal();

    // Mark as drafted by the selected team
    await markDrafted(playerId, false, teamDraftPosition);
}

// ==================== DRAFT BOARD MODAL ====================

// Show draft board modal with round-by-round view
async function showDraftBoard() {
    if (!draftSessionId) {
        alert('No active draft session');
        return;
    }

    const modal = document.getElementById('draft-board-modal');
    const content = document.getElementById('draft-board-content');

    content.innerHTML = '<div class="text-center py-8 text-gray-400">Loading draft board...</div>';
    modal.classList.remove('hidden');

    try {
        const response = await fetchAPI(`/draft/session/board?session_id=${draftSessionId}`);

        if (!response.picks || response.picks.length === 0) {
            content.innerHTML = '<div class="text-center py-8 text-gray-500">No picks made yet</div>';
            return;
        }

        // Group picks by round
        const rounds = {};
        response.picks.forEach(pick => {
            const round = pick.round_num || 1;
            if (!rounds[round]) rounds[round] = [];
            rounds[round].push(pick);
        });

        // Build the draft board table
        let html = '<div class="overflow-x-auto">';
        html += '<table class="w-full border-collapse">';

        // Header row with team names
        html += '<thead><tr class="border-b border-gray-700">';
        html += '<th class="p-2 text-left text-gray-400 text-sm">Round</th>';
        for (let i = 1; i <= draftNumTeams; i++) {
            const team = draftTeams.find(t => t.draft_position === i);
            const teamName = team ? team.name : `Team ${i}`;
            const isUser = i === draftUserPosition;
            html += `<th class="p-2 text-center text-sm ${isUser ? 'text-emerald-400 font-bold' : 'text-gray-400'}">${teamName}</th>`;
        }
        html += '</tr></thead>';

        // Body rows for each round
        html += '<tbody>';
        const maxRound = Math.max(...Object.keys(rounds).map(Number));

        for (let round = 1; round <= maxRound; round++) {
            const isSnakeReverse = round % 2 === 0;
            html += `<tr class="border-b border-gray-800/50 hover:bg-gray-700/20">`;
            html += `<td class="p-2 font-mono text-gray-500">${round}</td>`;

            for (let teamPos = 1; teamPos <= draftNumTeams; teamPos++) {
                // For snake draft, reverse order on even rounds
                const actualTeamPos = isSnakeReverse ? (draftNumTeams - teamPos + 1) : teamPos;
                const pick = (rounds[round] || []).find(p => p.team_draft_position === actualTeamPos);
                const isUser = actualTeamPos === draftUserPosition;

                if (pick) {
                    const cellClass = pick.is_user_pick ? 'bg-emerald-900/30 text-emerald-300' : 'text-gray-300';
                    html += `<td class="p-2 text-center text-sm ${cellClass}">${pick.player_name}</td>`;
                } else {
                    html += `<td class="p-2 text-center text-gray-600">-</td>`;
                }
            }

            html += '</tr>';
        }

        html += '</tbody></table></div>';
        content.innerHTML = html;

    } catch (error) {
        console.error('Failed to load draft board:', error);
        content.innerHTML = '<div class="text-center py-8 text-red-400">Failed to load draft board</div>';
    }
}

// Close draft board modal
function closeDraftBoardModal() {
    document.getElementById('draft-board-modal').classList.add('hidden');
}

// Override startQuickPractice to show session modal
const originalStartQuickPractice = startQuickPractice;
startQuickPractice = async function() {
    // Ask if they want to reset
    const draftedResponse = await fetchAPI('/players/?limit=500&available_only=false');
    const draftedCount = draftedResponse.filter(p => p.is_drafted).length;

    if (draftedCount > 0) {
        const reset = confirm(`${draftedCount} players are currently marked as drafted.\n\nWould you like to reset and start fresh?\n\nClick OK to reset, or Cancel to continue with current state.`);
        if (reset) {
            // Check if session is active first
            if (draftSessionId) {
                alert('Please end the current draft session before resetting.');
                return;
            }
            await resetDraft();
        }
    }

    // Close mock draft modal
    closeMockDraftModal();

    // Show the draft session modal
    showDraftSessionModal();
};

// ==================== ACHIEVEMENT SYSTEM ====================

// Check and unlock achievements based on pick
function checkAchievements(player) {
    if (!player) return;

    // First pick achievement
    if (myTeamPickCount === 1 && !unlockedAchievements.has('first_pick')) {
        unlockAchievement('first_pick');
    }

    // Position-based achievements
    const positions = player.positions || player.position || '';
    if (positions.includes('SP') && !unlockedAchievements.has('first_sp')) {
        unlockAchievement('first_sp');
    }
    if (positions.includes('RP') && !unlockedAchievements.has('first_rp')) {
        unlockAchievement('first_rp');
    }

    // Milestone achievements
    if (myTeamPickCount === 5 && !unlockedAchievements.has('five_picks')) {
        unlockAchievement('five_picks');
    }
    if (myTeamPickCount === 10 && !unlockedAchievements.has('ten_picks')) {
        unlockAchievement('ten_picks');
    }

    // Stat-based achievements (check projections if available)
    if (player.hr_proj >= 30 && !unlockedAchievements.has('power_hitter')) {
        unlockAchievement('power_hitter');
    }
    if (player.sb_proj >= 20 && !unlockedAchievements.has('speed_demon')) {
        unlockAchievement('speed_demon');
    }

    // Check for sleeper pick
    const classification = valueClassifications[player.id];
    if (classification === 'sleeper' && !unlockedAchievements.has('sleeper_pick')) {
        unlockAchievement('sleeper_pick');
    }
}

// ─── Achievement queue — prevents toasts from firing mid-pick ────────────────
const achievementQueue = [];
let achievementTimerId = null;

function queueAchievement(achievementKey) {
    achievementQueue.push(achievementKey);
    if (!achievementTimerId) {
        // During draft: delay 2s after last pick; outside draft: show immediately
        const delay = draftSessionId ? 2000 : 0;
        achievementTimerId = setTimeout(flushAchievementQueue, delay);
    }
}

function flushAchievementQueue() {
    achievementTimerId = null;
    const next = achievementQueue.shift();
    if (next) {
        const achievement = ACHIEVEMENTS[next];
        if (achievement) showAchievementToast(achievement);
    }
    if (achievementQueue.length > 0) {
        achievementTimerId = setTimeout(flushAchievementQueue, 3000);
    }
}
// ─────────────────────────────────────────────────────────────────────────────

// Unlock an achievement and show toast
function unlockAchievement(achievementId) {
    if (unlockedAchievements.has(achievementId)) return;

    unlockedAchievements.add(achievementId);
    queueAchievement(achievementId);
}

// Show achievement toast notification
function showAchievementToast(achievement) {
    // Remove any existing toast
    const existing = document.querySelector('.achievement-toast');
    if (existing) existing.remove();

    // Create toast element
    const toast = document.createElement('div');
    toast.className = 'achievement-toast';
    toast.innerHTML = `
        <div class="achievement-icon">${achievement.icon}</div>
        <div class="achievement-text">
            <div class="achievement-title">ACHIEVEMENT UNLOCKED</div>
            <div class="achievement-name">${achievement.name}</div>
        </div>
    `;

    document.body.appendChild(toast);

    // Remove after 4 seconds
    setTimeout(() => {
        toast.style.animation = 'achievementSlide 0.3s ease-in reverse';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

// Update XP bar display
function updateXPBar() {
    const xpBar = document.getElementById('xp-bar-fill');
    const xpText = document.getElementById('xp-text');

    if (!xpBar || !xpText) return;

    // Calculate progress (assume 23-man roster as "max")
    const maxPicks = 23;
    const progress = Math.min((myTeamPickCount / maxPicks) * 100, 100);

    xpBar.style.width = `${progress}%`;
    xpText.textContent = `${myTeamPickCount} / ${maxPicks} ROSTER`;
}

// Reset achievement state for new session
function resetAchievementState() {
    unlockedAchievements.clear();
    sessionPickCount = 0;
    myTeamPickCount = 0;
    updateXPBar();
}

// ==========================================
// PLAYER COMPARISON
// ==========================================

function toggleCompare(playerId, event) {
    if (event) event.stopPropagation();

    const idx = compareSlots.indexOf(playerId);
    if (idx !== -1) {
        // Remove from slots
        compareSlots[idx] = null;
    } else {
        // Add to first empty slot
        const emptyIdx = compareSlots.indexOf(null);
        if (emptyIdx !== -1) {
            compareSlots[emptyIdx] = playerId;
        } else {
            // Both full — replace slot 1 (keep slot 0)
            compareSlots[1] = playerId;
        }
    }
    updateCompareTray();
    renderPlayerList();
}

function updateCompareTray() {
    const tray = document.getElementById('compare-tray');
    const btn = document.getElementById('compare-btn');
    const hasAny = compareSlots[0] !== null || compareSlots[1] !== null;

    if (hasAny) {
        tray.classList.remove('hidden');
    } else {
        tray.classList.add('hidden');
        return;
    }

    // Update slot chips
    for (let i = 0; i < 2; i++) {
        const slotEl = document.getElementById(`compare-slot-${i}`);
        if (compareSlots[i] !== null) {
            const player = players.find(p => p.id === compareSlots[i]);
            const name = player ? player.name.split(' ').pop() : `#${compareSlots[i]}`;
            slotEl.className = 'compare-slot-filled';
            slotEl.innerHTML = `${escapeHtml(name)} <span onclick="removeFromCompare(${i}, event)" class="ml-1 cursor-pointer text-gray-500 hover:text-red-400">&times;</span>`;
        } else {
            slotEl.className = 'compare-slot-empty';
            slotEl.innerHTML = 'Empty';
        }
    }

    // Show compare button when 2 players selected
    if (compareSlots[0] !== null && compareSlots[1] !== null) {
        btn.classList.remove('hidden');
    } else {
        btn.classList.add('hidden');
    }
}

function removeFromCompare(slot, event) {
    if (event) event.stopPropagation();
    compareSlots[slot] = null;
    updateCompareTray();
    renderPlayerList();
}

function clearCompare() {
    compareSlots = [null, null];
    updateCompareTray();
    renderPlayerList();
}

function compareFromModal() {
    if (!currentModalPlayer) return;
    const pid = currentModalPlayer.id;

    const emptyIdx = compareSlots.indexOf(null);
    if (compareSlots.includes(pid)) {
        // Already in compare
    } else if (emptyIdx !== -1) {
        compareSlots[emptyIdx] = pid;
    } else {
        compareSlots[1] = pid;
    }

    updateCompareTray();
    renderPlayerList();

    // If both slots are full, auto-open
    if (compareSlots[0] !== null && compareSlots[1] !== null) {
        closeModal();
        openComparison();
    }
}

async function openComparison() {
    if (compareSlots[0] === null || compareSlots[1] === null) return;

    const modal = document.getElementById('compare-modal');
    const content = document.getElementById('compare-content');
    modal.classList.remove('hidden');
    content.innerHTML = '<div class="p-8 text-center text-gray-400">Loading comparison...</div>';

    try {
        const [playerA, playerB] = await Promise.all([
            fetchAPI(`/players/${compareSlots[0]}`),
            fetchAPI(`/players/${compareSlots[1]}`)
        ]);

        renderComparisonModal(playerA, playerB);
    } catch (error) {
        console.error('Failed to load comparison:', error);
        content.innerHTML = '<div class="p-8 text-center text-red-400">Failed to load player data</div>';
    }
}

function closeComparison() {
    document.getElementById('compare-modal').classList.add('hidden');
}

function renderComparisonModal(a, b) {
    const content = document.getElementById('compare-content');

    // Helper: determine winner for a stat row (higher is better by default)
    function winClass(valA, valB, lowerBetter = false) {
        if (valA == null || valB == null) return ['', ''];
        if (valA === valB) return ['', ''];
        const aWins = lowerBetter ? valA < valB : valA > valB;
        return aWins ? ['compare-winner', 'compare-loser'] : ['compare-loser', 'compare-winner'];
    }

    function statRow(label, valA, valB, opts = {}) {
        const fmt = opts.fmt || (v => v != null ? v : '--');
        const [clsA, clsB] = opts.lowerBetter
            ? winClass(valA, valB, true)
            : winClass(valA, valB);
        return `
            <div class="compare-stat-row">
                <div class="compare-stat-cell ${clsA}">${fmt(valA)}</div>
                <div class="compare-stat-label">${label}</div>
                <div class="compare-stat-cell ${clsB}">${fmt(valB)}</div>
            </div>`;
    }

    function fmtNum(v, decimals = 0) {
        if (v == null) return '--';
        return typeof v === 'number' ? v.toFixed(decimals) : v;
    }

    function fmtRate(v) { return v != null ? parseFloat(v).toFixed(3) : '--'; }

    // Average projections across sources
    function avgProj(player, field) {
        if (!player.projections || player.projections.length === 0) return null;
        const vals = player.projections.map(p => p[field]).filter(v => v != null && v !== 0);
        if (vals.length === 0) return null;
        return vals.reduce((a, b) => a + b, 0) / vals.length;
    }

    // Determine if batter or pitcher
    const aIsPitcher = (a.primary_position === 'SP' || a.primary_position === 'RP');
    const bIsPitcher = (b.primary_position === 'SP' || b.primary_position === 'RP');

    // Surplus values
    const surpA = surplusValues[a.id];
    const surpB = surplusValues[b.id];

    // Value classifications
    const valA = valueClassifications[a.id];
    const valB = valueClassifications[b.id];

    // Scarcity
    const scarcA = a.scarcity_context;
    const scarcB = b.scarcity_context;

    // Count winners for verdict
    let winsA = 0, winsB = 0;
    function tally(vA, vB, lowerBetter = false) {
        if (vA == null || vB == null) return;
        if (vA === vB) return;
        const aW = lowerBetter ? vA < vB : vA > vB;
        if (aW) winsA++; else winsB++;
    }

    // Tally rankings (lower rank = better)
    tally(a.consensus_rank, b.consensus_rank, true);
    tally(a.risk_score, b.risk_score, true);
    if (surpA && surpB) tally(surpA.surplus_value, surpB.surplus_value);

    // Projected stats tally
    const projFields = aIsPitcher && bIsPitcher
        ? [['ip',false],['wins',false],['strikeouts',false],['saves',false],['era',true],['whip',true],['quality_starts',false]]
        : [['pa',false],['runs',false],['hr',false],['rbi',false],['sb',false],['avg',false],['ops',false]];
    projFields.forEach(([f, lb]) => {
        tally(avgProj(a, f), avgProj(b, f), lb);
    });

    const verdictText = winsA === winsB
        ? 'Dead even across compared categories'
        : winsA > winsB
            ? `${a.name} leads in ${winsA} categories, ${b.name} in ${winsB}`
            : `${b.name} leads in ${winsB} categories, ${a.name} in ${winsA}`;

    // Build sections HTML
    let html = `
    <div class="p-6">
        <!-- Header -->
        <div class="flex justify-between items-center mb-6">
            <h2 class="text-xl font-bold text-cyan-400" style="font-family: 'Orbitron', sans-serif;">Player Comparison</h2>
            <button onclick="closeComparison()" class="text-gray-400 hover:text-white text-2xl">&times;</button>
        </div>

        <!-- Player Headers -->
        <div class="compare-stat-row" style="border-bottom: 2px solid rgba(6,182,212,0.3); padding-bottom: 12px; margin-bottom: 12px;">
            <div class="compare-stat-cell">
                <div class="text-lg font-bold text-white">${escapeHtml(a.name)}</div>
                <div class="text-xs text-gray-400">${a.team || 'FA'} | ${a.positions || '--'} | Age ${a.age || '--'}</div>
            </div>
            <div class="compare-stat-label text-cyan-400 font-bold">VS</div>
            <div class="compare-stat-cell">
                <div class="text-lg font-bold text-white">${escapeHtml(b.name)}</div>
                <div class="text-xs text-gray-400">${b.team || 'FA'} | ${b.positions || '--'} | Age ${b.age || '--'}</div>
            </div>
        </div>

        <!-- Rankings -->
        <div class="mb-4">
            <h3 class="text-sm font-bold text-gray-400 uppercase tracking-wider mb-2">Rankings</h3>
            ${statRow('Consensus Rank', a.consensus_rank, b.consensus_rank, {lowerBetter: true, fmt: v => v != null ? '#'+v : '--'})}
            ${statRow('Risk Score', a.risk_score, b.risk_score, {lowerBetter: true, fmt: v => fmtNum(v, 0)})}
        </div>

        <!-- Surplus Value -->
        <div class="mb-4">
            <h3 class="text-sm font-bold text-gray-400 uppercase tracking-wider mb-2">Surplus Value (VORP)</h3>
            ${statRow('Surplus', surpA?.surplus_value, surpB?.surplus_value, {fmt: v => v != null ? (v >= 0 ? '+' : '') + v.toFixed(1) : '--'})}
            ${statRow('Total Z-Score', surpA?.total_z, surpB?.total_z, {fmt: v => v != null ? v.toFixed(1) : '--'})}
            ${statRow('Position', surpA?.position_used, surpB?.position_used, {fmt: v => v || '--'})}
        </div>

        <!-- Projected Stats -->
        <div class="mb-4">
            <h3 class="text-sm font-bold text-gray-400 uppercase tracking-wider mb-2">Projected Stats (Avg)</h3>`;

    if (aIsPitcher && bIsPitcher) {
        html += statRow('IP', avgProj(a,'ip'), avgProj(b,'ip'), {fmt: v => fmtNum(v,0)});
        html += statRow('W', avgProj(a,'wins'), avgProj(b,'wins'), {fmt: v => fmtNum(v,0)});
        html += statRow('K', avgProj(a,'strikeouts'), avgProj(b,'strikeouts'), {fmt: v => fmtNum(v,0)});
        html += statRow('SV', avgProj(a,'saves'), avgProj(b,'saves'), {fmt: v => fmtNum(v,0)});
        html += statRow('ERA', avgProj(a,'era'), avgProj(b,'era'), {lowerBetter: true, fmt: v => fmtNum(v,2)});
        html += statRow('WHIP', avgProj(a,'whip'), avgProj(b,'whip'), {lowerBetter: true, fmt: v => fmtNum(v,2)});
        html += statRow('QS', avgProj(a,'quality_starts'), avgProj(b,'quality_starts'), {fmt: v => fmtNum(v,0)});
    } else {
        html += statRow('PA', avgProj(a,'pa'), avgProj(b,'pa'), {fmt: v => fmtNum(v,0)});
        html += statRow('R', avgProj(a,'runs'), avgProj(b,'runs'), {fmt: v => fmtNum(v,0)});
        html += statRow('HR', avgProj(a,'hr'), avgProj(b,'hr'), {fmt: v => fmtNum(v,0)});
        html += statRow('RBI', avgProj(a,'rbi'), avgProj(b,'rbi'), {fmt: v => fmtNum(v,0)});
        html += statRow('SB', avgProj(a,'sb'), avgProj(b,'sb'), {fmt: v => fmtNum(v,0)});
        html += statRow('AVG', avgProj(a,'avg'), avgProj(b,'avg'), {fmt: v => fmtRate(v)});
        html += statRow('OPS', avgProj(a,'ops'), avgProj(b,'ops'), {fmt: v => fmtRate(v)});
    }

    html += `</div>`;

    // Position Scarcity
    html += `<div class="mb-4">
        <h3 class="text-sm font-bold text-gray-400 uppercase tracking-wider mb-2">Position Scarcity</h3>
        ${statRow('Scarcity', scarcA?.scarcity_multiplier, scarcB?.scarcity_multiplier, {fmt: v => v != null ? v.toFixed(2) + 'x' : '--'})}
        ${statRow('Tier', scarcA?.tier, scarcB?.tier, {fmt: v => v || '--'})}
    </div>`;

    // Value Classification
    html += `<div class="mb-4">
        <h3 class="text-sm font-bold text-gray-400 uppercase tracking-wider mb-2">Value Tag</h3>
        ${statRow('Classification', valA?.classification, valB?.classification, {fmt: v => v ? v.replace('_', ' ').toUpperCase() : 'FAIR VALUE'})}
    </div>`;

    // Verdict
    const verdictColorA = winsA > winsB ? 'text-emerald-400' : winsA < winsB ? 'text-gray-500' : 'text-yellow-400';
    const verdictColorB = winsB > winsA ? 'text-emerald-400' : winsB < winsA ? 'text-gray-500' : 'text-yellow-400';

    html += `
        <!-- Verdict -->
        <div class="mt-6 p-4 bg-gray-900/50 rounded-lg border border-cyan-500/20">
            <div class="compare-stat-row">
                <div class="compare-stat-cell ${verdictColorA} text-2xl font-bold">${winsA}</div>
                <div class="compare-stat-label text-sm text-gray-400">Category Wins</div>
                <div class="compare-stat-cell ${verdictColorB} text-2xl font-bold">${winsB}</div>
            </div>
            <p class="text-center text-sm text-gray-400 mt-2">${verdictText}</p>
        </div>
    </div>`;

    content.innerHTML = html;
}

// ==================== ESPN Settings ====================

function checkCredentialStatus(leagueData) {
    const banner = document.getElementById('creds-warning-banner');
    if (!banner) return;
    if (leagueData && leagueData.has_espn_credentials) {
        banner.classList.add('hidden');
    } else {
        banner.classList.remove('hidden');
    }
}

function showSettingsModal() {
    const modal = document.getElementById('settings-modal');
    const s2Input = document.getElementById('settings-espn-s2');
    const swidInput = document.getElementById('settings-swid');
    const resultDiv = document.getElementById('settings-test-result');

    // Clear previous state
    s2Input.value = '';
    swidInput.value = '';
    resultDiv.classList.add('hidden');
    resultDiv.textContent = '';

    // Show placeholder hint if creds already configured
    const banner = document.getElementById('creds-warning-banner');
    if (banner && banner.classList.contains('hidden')) {
        s2Input.placeholder = '\u2022\u2022\u2022\u2022\u2022\u2022 (credentials already saved — enter new values to update)';
        swidInput.placeholder = '\u2022\u2022\u2022\u2022\u2022\u2022 (credentials already saved — enter new values to update)';
    } else {
        s2Input.placeholder = 'Paste your espn_s2 cookie value here...';
        swidInput.placeholder = 'Paste your SWID cookie value here (e.g. {GUID})';
    }

    modal.classList.remove('hidden');
}

// ─── First-Run Setup Wizard ────────────────────────────────────────────────

function showSetupWizard(leagueData) {
    const modal = document.getElementById('setup-wizard-modal');
    if (!modal) return;
    // Pre-fill league name if it's already set to something meaningful
    if (leagueData && leagueData.name) {
        document.getElementById('wizard-league-name').value = leagueData.name;
    }
    modal.classList.remove('hidden');
}

function skipSetupWizard() {
    localStorage.setItem('setupSkipped', 'true');
    document.getElementById('setup-wizard-modal').classList.add('hidden');
}

async function saveSetupWizard() {
    if (!currentLeagueId) return;

    const nameInput = document.getElementById('wizard-league-name').value.trim();
    const leagueIdInput = parseInt(document.getElementById('wizard-espn-league-id').value, 10);
    const espnS2 = document.getElementById('wizard-espn-s2').value.trim();
    const swid = document.getElementById('wizard-swid').value.trim();
    const errorEl = document.getElementById('wizard-error');
    const saveBtn = document.getElementById('wizard-save-btn');

    errorEl.classList.add('hidden');

    if (!leagueIdInput || leagueIdInput <= 0) {
        errorEl.textContent = 'Please enter a valid ESPN League ID.';
        errorEl.classList.remove('hidden');
        return;
    }

    saveBtn.disabled = true;
    saveBtn.textContent = 'Saving…';

    try {
        const patchBody = { espn_league_id: leagueIdInput };
        if (nameInput) patchBody.name = nameInput;

        const updated = await fetchAPI(`/leagues/${currentLeagueId}`, {
            method: 'PATCH',
            body: JSON.stringify(patchBody)
        });

        // Update header league name
        const displayName = updated.name || nameInput || 'My Fantasy League';
        document.getElementById('league-name').textContent = displayName;
        document.getElementById('league-name-badge').classList.remove('hidden');

        // Save credentials if provided
        if (espnS2 && swid) {
            await fetchAPI(`/leagues/${currentLeagueId}/credentials`, {
                method: 'POST',
                body: JSON.stringify({ espn_s2: espnS2, swid: swid })
            });
            // Trigger ESPN sync
            fetchAPI(`/leagues/${currentLeagueId}/sync`, { method: 'POST' }).catch(() => {});
        }

        localStorage.removeItem('setupSkipped');
        document.getElementById('setup-wizard-modal').classList.add('hidden');
    } catch (err) {
        errorEl.textContent = 'Failed to save. Please try again.';
        errorEl.classList.remove('hidden');
    } finally {
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save & Continue';
    }
}

// ──────────────────────────────────────────────────────────────────────────

function closeSettingsModal() {
    document.getElementById('settings-modal').classList.add('hidden');
}

async function testESPNCredentials() {
    if (!currentLeagueId) return;

    const s2 = document.getElementById('settings-espn-s2').value.trim();
    const swid = document.getElementById('settings-swid').value.trim();
    const resultDiv = document.getElementById('settings-test-result');

    resultDiv.classList.remove('hidden');
    resultDiv.className = 'rounded-lg p-3 text-sm bg-gray-700 text-gray-300';
    resultDiv.textContent = 'Testing connection...';

    try {
        const body = (s2 && swid) ? { espn_s2: s2, swid: swid } : null;
        const result = await fetchAPI(`/leagues/${currentLeagueId}/credentials/test`, {
            method: 'POST',
            body: body ? JSON.stringify(body) : JSON.stringify(null),
        });

        if (result.status === 'valid') {
            resultDiv.className = 'rounded-lg p-3 text-sm bg-green-900/50 border border-green-600 text-green-300';
            resultDiv.textContent = `Connected to "${escapeHtml(result.league_name)}"`;
        } else {
            resultDiv.className = 'rounded-lg p-3 text-sm bg-red-900/50 border border-red-600 text-red-300';
            resultDiv.textContent = `Connection failed: ${escapeHtml(result.error)}`;
        }
    } catch (error) {
        resultDiv.className = 'rounded-lg p-3 text-sm bg-red-900/50 border border-red-600 text-red-300';
        resultDiv.textContent = `Error: ${escapeHtml(error.message)}`;
    }
}

async function saveESPNCredentials() {
    if (!currentLeagueId) return;

    const s2 = document.getElementById('settings-espn-s2').value.trim();
    const swid = document.getElementById('settings-swid').value.trim();

    if (!s2 || !swid) {
        const resultDiv = document.getElementById('settings-test-result');
        resultDiv.classList.remove('hidden');
        resultDiv.className = 'rounded-lg p-3 text-sm bg-red-900/50 border border-red-600 text-red-300';
        resultDiv.textContent = 'Both espn_s2 and SWID are required to save.';
        return;
    }

    try {
        await fetchAPI(`/leagues/${currentLeagueId}/credentials`, {
            method: 'POST',
            body: JSON.stringify({ espn_s2: s2, swid: swid }),
        });

        // Hide warning banner
        const banner = document.getElementById('creds-warning-banner');
        if (banner) banner.classList.add('hidden');

        // Show success in the result div
        const resultDiv = document.getElementById('settings-test-result');
        resultDiv.classList.remove('hidden');
        resultDiv.className = 'rounded-lg p-3 text-sm bg-green-900/50 border border-green-600 text-green-300';
        resultDiv.textContent = 'Credentials saved successfully!';

        // Close modal after a brief delay
        setTimeout(() => closeSettingsModal(), 1200);
    } catch (error) {
        const resultDiv = document.getElementById('settings-test-result');
        resultDiv.classList.remove('hidden');
        resultDiv.className = 'rounded-lg p-3 text-sm bg-red-900/50 border border-red-600 text-red-300';
        resultDiv.textContent = `Failed to save: ${escapeHtml(error.message)}`;
    }
}

async function autoDetectESPNCredentials() {
    const btn = document.getElementById('auto-detect-btn');
    const resultDiv = document.getElementById('settings-test-result');

    btn.disabled = true;
    btn.textContent = 'Detecting...';
    resultDiv.classList.remove('hidden');
    resultDiv.className = 'rounded-lg p-3 text-sm bg-gray-700 text-gray-300';
    resultDiv.textContent = 'Reading Chrome cookies...';

    try {
        const result = await fetchAPI('/leagues/credentials/auto-detect', {
            method: 'POST',
        });

        if (result.status === 'found') {
            document.getElementById('settings-espn-s2').value = result.espn_s2;
            document.getElementById('settings-swid').value = result.swid;
            resultDiv.className = 'rounded-lg p-3 text-sm bg-green-900/50 border border-green-600 text-green-300';
            resultDiv.textContent = 'Credentials detected! Click "Save Credentials" to apply.';
        } else {
            resultDiv.className = 'rounded-lg p-3 text-sm bg-red-900/50 border border-red-600 text-red-300';
            resultDiv.textContent = result.error;
            // Populate any partial results
            if (result.espn_s2) document.getElementById('settings-espn-s2').value = result.espn_s2;
            if (result.swid) document.getElementById('settings-swid').value = result.swid;
        }
    } catch (error) {
        resultDiv.className = 'rounded-lg p-3 text-sm bg-red-900/50 border border-red-600 text-red-300';
        resultDiv.textContent = `Auto-detect failed: ${escapeHtml(error.message)}`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<span>&#x1F50D;</span> Auto-Detect from Chrome';
    }
}


// ============================================================================
// Keeper Management Functions
// ============================================================================

// Load keepers for the current league
async function loadKeepers() {
    if (!currentLeagueId) return;

    try {
        keepers = await fetchAPI(`/keepers/${currentLeagueId}`);
        // Extract unique team names for datalist and draft pre-population
        keeperTeamNames = [...new Set(keepers.map(k => k.team_name))];

        renderKeepersDisplay();
        updateKeeperTeamSelector();
        updateKeeperRoundOptions();

        // Update count
        const countEl = document.getElementById('keeper-count');
        if (countEl) countEl.textContent = `${keepers.length} keeper${keepers.length !== 1 ? 's' : ''}`;

        // Initialize round select if empty
        const roundSelect = document.getElementById('keeper-round-select');
        if (roundSelect && roundSelect.options.length === 0) {
            for (let i = 1; i <= 25; i++) {
                const opt = document.createElement('option');
                opt.value = i;
                opt.textContent = `Round ${i}`;
                roundSelect.appendChild(opt);
            }
        }
    } catch (error) {
        console.error('Failed to load keepers:', error);
    }
}

// Render keepers grouped by team
function renderKeepersDisplay() {
    const container = document.getElementById('keepers-display');
    if (!container) return;

    if (keepers.length === 0) {
        container.innerHTML = `
            <div class="text-center py-4">
                <p class="text-gray-500 text-sm">No keepers added yet</p>
                <p class="text-xs text-gray-600 mt-1">Add keepers to lock them into draft rounds</p>
            </div>
        `;
        return;
    }

    // Group by team
    const grouped = {};
    for (const k of keepers) {
        if (!grouped[k.team_name]) grouped[k.team_name] = [];
        grouped[k.team_name].push(k);
    }

    let html = '';
    for (const [teamName, teamKeepers] of Object.entries(grouped)) {
        teamKeepers.sort((a, b) => a.keeper_round - b.keeper_round);
        html += `
            <div class="keeper-team-group">
                <div class="flex items-center justify-between mb-2">
                    <span class="text-sm font-semibold text-amber-300">${escapeHtml(teamName)}</span>
                    <span class="text-xs text-gray-500">${teamKeepers.length} keeper${teamKeepers.length !== 1 ? 's' : ''}</span>
                </div>
                ${teamKeepers.map(k => `
                    <div class="keeper-card">
                        <span class="keeper-round-badge">R${k.keeper_round}</span>
                        <div class="flex-1 min-w-0">
                            <span class="text-sm text-gray-200 truncate block">${escapeHtml(k.player_name)}</span>
                            ${k.player_positions ? `<span class="text-xs text-gray-500">${escapeHtml(k.player_positions)}</span>` : ''}
                        </div>
                        <button onclick="removeKeeper(${k.id})" class="text-gray-600 hover:text-red-400 transition-colors flex-shrink-0" title="Remove keeper">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path>
                            </svg>
                        </button>
                    </div>
                `).join('')}
            </div>
        `;
    }
    container.innerHTML = html;
}

// Build team options for keeper selector from Teams tab/session config.
function getKeeperTeamOptions() {
    const names = new Set();

    // 1) Active draft session teams
    for (const t of (draftTeams || [])) {
        if (t?.name) names.add(t.name.trim());
    }

    // 2) Saved Teams tab / draft config names
    const saved = loadDraftConfig();
    for (const n of (saved?.teamNames || [])) {
        if (n) names.add(String(n).trim());
    }

    // 3) Existing keepers (fallback)
    for (const n of (keeperTeamNames || [])) {
        if (n) names.add(String(n).trim());
    }

    return [...names].filter(Boolean).sort((a, b) => a.localeCompare(b));
}

// Update team selector for keepers from known team names.
function updateKeeperTeamSelector() {
    const select = document.getElementById('keeper-team-select');
    if (!select) return;

    const previousValue = select.value;
    const options = getKeeperTeamOptions();
    select.innerHTML = '<option value="">Select a team...</option>';

    for (const name of options) {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        select.appendChild(opt);
    }

    if (previousValue && options.includes(previousValue)) {
        select.value = previousValue;
    }

    updateKeeperRoundOptions();
}

// Update round options - disable rounds already used by the selected team
function updateKeeperRoundOptions() {
    const teamInput = document.getElementById('keeper-team-select');
    const roundSelect = document.getElementById('keeper-round-select');
    if (!teamInput || !roundSelect) return;

    const teamName = teamInput.value.trim().toLowerCase();
    const usedRounds = new Set(
        keepers
            .filter(k => k.team_name.toLowerCase() === teamName)
            .map(k => k.keeper_round)
    );

    for (const opt of roundSelect.options) {
        const round = parseInt(opt.value);
        opt.disabled = usedRounds.has(round);
        opt.textContent = usedRounds.has(round) ? `Round ${round} (taken)` : `Round ${round}`;
    }
}

// Debounced player search for keeper autocomplete
function searchKeeperPlayer(query) {
    clearTimeout(keeperSearchTimeout);
    const dropdown = document.getElementById('keeper-autocomplete');

    if (!query || query.length < 2) {
        dropdown.classList.add('hidden');
        return;
    }

    keeperSearchTimeout = setTimeout(async () => {
        try {
            const results = await fetchAPI(
                `/players/search?q=${encodeURIComponent(query)}&available_only=true&limit=8`
            );

            if (results.length === 0) {
                dropdown.innerHTML = '<div class="keeper-autocomplete-item text-gray-500">No players found</div>';
            } else {
                dropdown.innerHTML = results.map(p => `
                    <div class="keeper-autocomplete-item" data-player-id="${p.id}" data-player-name="${escapeHtml(p.name)}">
                        <span class="text-gray-200">${escapeHtml(p.name)}</span>
                        <span class="text-xs text-gray-500 ml-2">${escapeHtml(p.positions || '')} - ${escapeHtml(p.team || '')}</span>
                    </div>
                `).join('');
                // Attach click handlers via delegation
                dropdown.querySelectorAll('.keeper-autocomplete-item[data-player-id]').forEach(item => {
                    item.addEventListener('click', () => {
                        selectKeeperPlayer(
                            parseInt(item.dataset.playerId),
                            item.dataset.playerName
                        );
                    });
                });
            }
            dropdown.classList.remove('hidden');
        } catch (error) {
            console.error('Keeper player search failed:', error);
            dropdown.classList.add('hidden');
        }
    }, 300);
}

// Select a player from the autocomplete dropdown
function selectKeeperPlayer(playerId, playerName) {
    selectedKeeperPlayerId = playerId;
    document.getElementById('keeper-player-input').value = playerName;
    document.getElementById('keeper-player-id').value = playerId;
    document.getElementById('keeper-autocomplete').classList.add('hidden');
}

// Add a keeper
async function addKeeper() {
    const teamName = document.getElementById('keeper-team-select').value.trim();
    const playerId = parseInt(document.getElementById('keeper-player-id').value) || 0;
    const round = parseInt(document.getElementById('keeper-round-select').value) || 0;

    if (!teamName) {
        alert('Please select a team');
        return;
    }
    if (!playerId) {
        alert('Please search and select a player');
        return;
    }
    if (!round) {
        alert('Please select a round');
        return;
    }

    try {
        await fetchAPI(`/keepers/${currentLeagueId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                team_name: teamName,
                player_id: playerId,
                keeper_round: round,
            }),
        });

        // Reset form (keep team name for adding multiple keepers to same team)
        document.getElementById('keeper-player-input').value = '';
        document.getElementById('keeper-player-id').value = '';
        document.getElementById('keeper-round-select').selectedIndex = 0;
        selectedKeeperPlayerId = null;

        // Reload keepers and player list
        await loadKeepers();
        await loadPlayers();
    } catch (error) {
        alert('Failed to add keeper: ' + (error.message || 'Unknown error'));
    }
}

// Remove a keeper
async function removeKeeper(keeperId) {
    if (!confirm('Remove this keeper?')) return;

    try {
        await fetchAPI(`/keepers/${currentLeagueId}/${keeperId}`, {
            method: 'DELETE',
        });

        await loadKeepers();
        await loadPlayers();
    } catch (error) {
        alert('Failed to remove keeper: ' + (error.message || 'Unknown error'));
    }
}

// Listen for team name changes to update round options
document.addEventListener('DOMContentLoaded', () => {
    const teamInput = document.getElementById('keeper-team-select');
    if (teamInput) {
        teamInput.addEventListener('change', updateKeeperRoundOptions);
    }

    // Close keeper autocomplete when clicking outside
    document.addEventListener('click', (e) => {
        const dropdown = document.getElementById('keeper-autocomplete');
        const input = document.getElementById('keeper-player-input');
        if (dropdown && input && !dropdown.contains(e.target) && e.target !== input) {
            dropdown.classList.add('hidden');
        }
    });
});

// Test function for loading state - can be called from browser console
async function testLoadingState() {
    console.log('Testing loading state...');
    try {
        // This will trigger the loading state
        await loadPlayers();
        console.log('Loading state test completed');
    } catch (error) {
        console.error('Test failed:', error);
    }
}

// Test function for error handling - can be called from browser console
async function testErrorHandling() {
    console.log('Testing error handling...');
    try {
        // This will trigger an error (invalid endpoint)
        await fetchAPI('/invalid-endpoint');
    } catch (error) {
        console.log('Error handling test completed');
    }
}

// Test function for network error simulation
async function testNetworkError() {
    console.log('Testing network error handling...');
    try {
        // This will simulate a network error
        await fetchAPI('http://invalid-domain-that-does-not-exist-12345.com/api/test');
    } catch (error) {
        console.log('Network error handling test completed');
    }
}
