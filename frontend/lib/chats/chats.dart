// lib/chats/chats_page.dart
import 'dart:async';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:frontend/chats/bubble_chats.dart';
import 'package:frontend/chats/inputbar.dart';
import 'package:frontend/chats/service_chats.dart';
import 'package:frontend/chats/sidebar.dart';
import 'package:go_router/go_router.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:frontend/chats/batik_theme.dart';

// ---------------------------------------------------------------------------
// Greetings
// ---------------------------------------------------------------------------

const _kGreetings = [
  'Halo! Ada yang bisa Ragna bantu hari ini? 🔍',
  'Selamat datang! Ragna siap membantu menjawab pertanyaan Anda.',
  'Hai! Ragna di sini, siap membantu dengan pengetahuan yang terhubung.',
  'Halo! Ragna dapat membantu Anda menjelajahi informasi dengan mudah.',
  'Selamat datang kembali! Ragna siap membantu Anda hari ini.',
  'Hai! Ragna — asisten cerdas Anda untuk segala pertanyaan. 🧠',
  'Halo! Butuh bantuan? Ragna siap membantu dengan pengetahuan yang luas.',
];

// ---------------------------------------------------------------------------
// Platform Helpers
// ---------------------------------------------------------------------------

bool _isMobileDevice(BuildContext context) {
  final width = MediaQuery.of(context).size.width;
  return width < 768 ||
      (!kIsWeb &&
          (defaultTargetPlatform == TargetPlatform.android ||
              defaultTargetPlatform == TargetPlatform.iOS));
}

// ---------------------------------------------------------------------------
// ChatsPage
// ---------------------------------------------------------------------------

class ChatsPage extends StatefulWidget {
  const ChatsPage({super.key});

  @override
  State<ChatsPage> createState() => _ChatsPageState();
}

class _ChatsPageState extends State<ChatsPage>
    with SingleTickerProviderStateMixin {
  late final ChatService _chatService;

  bool _sidebarOpen = false; 
  bool _sidebarInitialized = false;
  late final AnimationController _sidebarCtrl;
  late final Animation<double> _sidebarAnim;

  List<ChatTopic> _topics = [];
  ChatUserProfile? _profile;
  bool _loadingTopics = true;

  int? _activeChatId;
  List<ChatMessage> _messages = [];
  bool _loadingMessages = false;

  bool _sending = false;
  String? _pendingQuestion;
  Uint8List? _pendingImageBytes; // <--- STATE GAMBAR

  final Map<int, SseTracker> _trackers = {};

  int? _renamingId;
  String? _renamingTemp;
  String? _greeting;

  final _inputCtrl = TextEditingController();
  final _scrollCtrl = ScrollController();
  final _inputFocus = FocusNode();

  // ── Question Navigator ────────────────────────────────────────────────────
  bool _questionNavOpen = false;
  final Map<int, GlobalKey> _messageKeys = {};

  final GlobalKey _inputBarKey = GlobalKey();

  @override
  void initState() {
    super.initState();

    _sidebarCtrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 250),
      value: 1.0,
    );
    _sidebarAnim = CurvedAnimation(
      parent: _sidebarCtrl,
      curve: Curves.easeInOut,
    );

    _chatService = ChatService(
      onForceLogout: _handleForceLogout,
      onTokenUpdated: (_) => setState(() {}),
    );

    _initAuth();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (!_sidebarInitialized) {
      _sidebarInitialized = true;
      final isMobile = MediaQuery.of(context).size.width < 768;
      _sidebarOpen = !isMobile; 
      if (_sidebarOpen) {
        _sidebarCtrl.value = 1.0;
      } else {
        _sidebarCtrl.value = 0.0;
      }
    }
  }

  @override
  void dispose() {
    _cancelAllTrackers();
    _chatService.dispose();
    _sidebarCtrl.dispose();
    _inputCtrl.dispose();
    _scrollCtrl.dispose();
    _inputFocus.dispose();
    super.dispose();
  }

  // ── Auth ──────────────────────────────────────────────────────────────────

  Future<void> _initAuth() async {
    final isAuthenticated = await _chatService.initAuth();
    if (!isAuthenticated) {
      if (mounted) context.go('/users/login');
      return;
    }
    await _chatService.initModelPreference();

    await Future.wait([_fetchTopics(), _fetchProfile()]);
    _pickGreeting();
    
    // ✅ Panggil inisialisasi InputBar setelah auth selesai
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final inputBar = _inputBarKey.currentWidget as InputBar?;
      if (inputBar != null) {
        // Call method via state
        (_inputBarKey.currentState as dynamic)?.initializeAfterAuth();
      }
    });
  }

  void _handleForceLogout() {
    if (mounted) context.go('/users/login');
  }

  Future<void> _logout() async {
    _cancelAllTrackers();
    await _chatService.logout();
    if (mounted) context.go('/users/login');
  }

  // ── SSE Tracker ───────────────────────────────────────────────────────────

  void _startTracking(int detailId) {
    _trackers[detailId]?.cancel();
    final tracker = SseTracker(detailId: detailId);
    _trackers[detailId] = tracker;

    tracker.sseSub = _chatService
        .subscribeToStream(detailId)
        .listen(
          (event) async {
            if (!mounted) return;

            if (event.type == 'done' ||
                event.type == 'error' ||
                event.type == 'stopped') {
              await _fetchAndApplyMessage(detailId);
              _stopTracking(detailId);
            } else if (event.type == 'timeout') {
              _markDisconnected(detailId);
              _stopTracking(detailId);
            }
          },
          onError: (error) {
            if (mounted) _markDisconnected(detailId);
            _stopTracking(detailId);
          },
          onDone: () {
            if (mounted) {
              final msg = _messages.firstWhere(
                (m) => m.id == detailId,
                orElse: () => ChatMessage(
                  id: detailId,
                  chatId: 0,
                  question: '',
                  response: '',
                  processingStatus: 'pending',
                  createdAt: '',
                ),
              );
              if (msg.isPending) _markDisconnected(detailId);
            }
            _stopTracking(detailId);
          },
          cancelOnError: true,
        );

    Future.delayed(const Duration(seconds: 30), () {
      if (mounted && _trackers.containsKey(detailId)) {
        final msg = _messages.firstWhere(
          (m) => m.id == detailId,
          orElse: () => ChatMessage(
            id: detailId,
            chatId: 0,
            question: '',
            response: '',
            processingStatus: 'pending',
            createdAt: '',
          ),
        );
        if (msg.isPending) _fetchAndApplyMessage(detailId);
      }
    });
  }

  Future<void> _fetchAndApplyMessage(int detailId) async {
    if (!mounted) return;
    final updated = await _chatService.fetchMessage(detailId);
    if (updated != null) {
      _applyMessageUpdate(updated);
    } else {
      _markDisconnected(detailId);
    }
  }

  void _applyMessageUpdate(ChatMessage updated) {
    if (!mounted) return;
    final idx = _messages.indexWhere((m) => m.id == updated.id);
    if (idx != -1) {
      // Pertahankan gambar lokal agar tidak hilang
      final currentLocalImg = _messages[idx].localImageBytes;
      if (currentLocalImg != null) {
        updated.localImageBytes = currentLocalImg;
      }

      setState(() => _messages[idx] = updated);
      _scrollToBottom();
    }
  }

  void _markDisconnected(int detailId) {
    if (!mounted) return;
    final idx = _messages.indexWhere((m) => m.id == detailId);
    if (idx != -1) {
      setState(() => _messages[idx].processingStatus = 'disconnected');
    }
  }

  void _stopTracking(int detailId) {
    _trackers[detailId]?.cancel();
    _trackers.remove(detailId);
    if (mounted) setState(() {});
  }

  void _cancelAllTrackers() {
    for (final t in _trackers.values) {
      t.cancel();
    }
    _trackers.clear();
  }

  // ── Fetch ─────────────────────────────────────────────────────────────────

  Future<void> _fetchTopics() async {
    final topics = await _chatService.fetchTopics();
    if (mounted) {
      setState(() {
        _topics = topics;
        _loadingTopics = false;
      });
    }
  }

  Future<void> _fetchProfile() async {
    final profile = await _chatService.fetchProfile();
    if (mounted && profile != null) setState(() => _profile = profile);
  }

  Future<void> _fetchMessages(int chatId) async {
    setState(() {
      _loadingMessages = true;
      _messages = [];
    });

    final msgs = await _chatService.fetchMessages(chatId);

    if (!mounted) return;

    setState(() {
      _messages = msgs;
      _loadingMessages = false;
    });
    _syncMessageKeys();
    _scrollToBottom();

    for (final msg in msgs.where((m) => m.isPending)) {
      _startTracking(msg.id);
    }
  }

  // ── Actions ───────────────────────────────────────────────────────────────

  void _pickGreeting() {
    setState(
      () => _greeting = _kGreetings[Random().nextInt(_kGreetings.length)],
    );
  }

  void _newChat() {
    _cancelAllTrackers();
    _pickGreeting();
    setState(() {
      _activeChatId = null;
      _messages = [];
      _pendingQuestion = null;
      _pendingImageBytes = null;
      _questionNavOpen = false;
    });
  }

  void _selectTopic(ChatTopic topic) {
    _cancelAllTrackers();
    setState(() {
      _activeChatId = topic.id;
      _greeting = null;
      _pendingQuestion = null;
      _pendingImageBytes = null;
      _questionNavOpen = false;
    });
    _fetchMessages(topic.id);
    if (MediaQuery.of(context).size.width < 768) _toggleSidebar();
  }

  Future<void> _sendMessage({
    String? overrideText,
    int? replaceDetailId,
    Uint8List? imageBytes,
    String? imageName,
  }) async {
    final text = overrideText ?? _inputCtrl.text.trim();
    if ((text.isEmpty && imageBytes == null) || _sending) return;

    if (overrideText == null) _inputCtrl.clear();
    setState(() {
      _sending = true;
      _pendingQuestion = replaceDetailId == null 
          ? (text.isEmpty ? 'Tolong jelaskan gambar tanaman ini.' : text) 
          : null;
      _pendingImageBytes = replaceDetailId == null ? imageBytes : null;
    });
    _scrollToBottom();

    final msg = await _chatService.sendMessage(
      chatId: _activeChatId,
      question: text,
      imageBytes: imageBytes,
      imageName: imageName,
    );

    if (msg == null) {
      setState(() {
        _pendingQuestion = null;
        _pendingImageBytes = null;
        _sending = false;
      });
      _showSnack('Gagal mengirim pesan. Coba lagi.');
      return;
    }

    if (_activeChatId == null) {
      setState(() {
        _activeChatId = msg.chatId;
        _greeting = null;
        _pendingQuestion = null;
        _pendingImageBytes = null;
        _messages.add(msg);
        _sending = false;
      });
      _syncMessageKeys();
      await _fetchTopics();
    } else if (replaceDetailId != null) {
      final idx = _messages.indexWhere((m) => m.id == replaceDetailId);
      setState(() {
        _pendingQuestion = null;
        _pendingImageBytes = null;
        if (idx != -1) {
          _messages[idx] = msg;
        } else {
          _messages.add(msg);
        }
        _sending = false;
      });
      _syncMessageKeys();
    } else {
      setState(() {
        _pendingQuestion = null;
        _pendingImageBytes = null;
        _messages.add(msg);
        _sending = false;
      });
      _syncMessageKeys();
    }

    _scrollToBottom();
    _startTracking(msg.id);
  }

  Future<void> _uploadPdfs(
    List<PdfUploadFile> files, {
    void Function(int done, int total)? onProgress,
  }) async {
    final results = await _chatService.uploadPdfs(
      files: files,
      onProgress: onProgress,
    );
    if (!mounted) return;

    final successCount = results.where((r) => r?['success'] == true).length;
    final failCount = results.length - successCount;

    String message;
    Color color;
    if (failCount == 0) {
      message = successCount == 1
          ? 'PDF "${files.first.name}" diterima dan sedang diproses.'
          : '$successCount PDF berhasil diterima dan sedang diproses.';
      color = BatikColors.hijau;
    } else if (successCount == 0) {
      final firstFail = results.firstWhere((r) => r?['success'] != true);
      message =
          firstFail?['detail'] as String? ??
          firstFail?['message'] as String? ??
          'Gagal mengunggah semua file.';
      color = BatikColors.marun;
    } else {
      message =
          '$successCount berhasil, $failCount gagal. Periksa ulang file yang gagal.';
      color = BatikColors.sogaMuda;
    }

    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(message, style: GoogleFonts.nunitoSans(fontSize: 13, color: Colors.white)),
        backgroundColor: color,
        behavior: SnackBarBehavior.floating,
        duration: const Duration(seconds: 5),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
    );
  }

  Future<void> _editMessage(ChatMessage msg, String newQuestion) async {
    if (_sending) return;
    setState(() => _sending = true);

    final updated = await _chatService.editMessage(msg.id, newQuestion);

    if (updated != null) {
      final idx = _messages.indexWhere((m) => m.id == msg.id);
      if (idx != -1) setState(() => _messages[idx] = updated);
      _startTracking(msg.id);
    } else {
      _showSnack('Gagal mengedit pesan.');
    }

    if (mounted) setState(() => _sending = false);
  }

  Future<void> _regenerateResponse(ChatMessage msg) async {
    if (_sending) return;
    setState(() => _sending = true);

    final updated = await _chatService.regenerateResponse(msg.id);

    if (updated != null) {
      final idx = _messages.indexWhere((m) => m.id == msg.id);
      if (idx != -1) setState(() => _messages[idx] = updated);
      _startTracking(msg.id);
    } else {
      _showSnack('Gagal regenerate jawaban.');
    }

    if (mounted) setState(() => _sending = false);
  }

  Future<void> _stopGeneration(int detailId) async {
    final success = await _chatService.stopGeneration(detailId);
    if (!success && mounted) {
      _showSnack('Gagal menghentikan generate.');
    }
  }

  Future<void> _copyText(String text) async {
    await Clipboard.setData(ClipboardData(text: text));
    _showSnack('Disalin ke clipboard.');
  }

  Future<void> _playTTS(ChatMessage msg) async {
    if (msg.response.isEmpty) {
      _showSnack('Belum ada jawaban untuk dibacakan.');
      return;
    }
    _showSnack('Memuat suara...');

    try {
      await _chatService.playTTS(msg.id);
    } catch (e) {
      _showSnack('Gagal memutar suara.');
    }
  }

  Future<void> _stopTTS() async {
    try {
      await _chatService.stopTTS();
    } catch (e) {
      _showSnack('Gagal menghentikan suara.');
    }
  }

  Future<void> _resendMessage(ChatMessage msg) async {
    await _sendMessage(
      overrideText: msg.question, 
      replaceDetailId: msg.id,
      imageBytes: msg.localImageBytes, // Kirim ulang gambar jika putus koneksi
    );
  }

  Future<void> _deleteTopic(ChatTopic topic) async {
    final success = await _chatService.deleteTopic(topic.id);
    if (success) {
      _cancelAllTrackers();
      setState(() {
        _topics.removeWhere((t) => t.id == topic.id);
        if (_activeChatId == topic.id) {
          _activeChatId = null;
          _messages = [];
          _pendingQuestion = null;
          _pendingImageBytes = null;
          _pickGreeting();
        }
      });
    } else {
      _showSnack('Gagal menghapus topik.');
    }
  }

  Future<void> _renameTopic(ChatTopic topic, String newTitle) async {
    final trimmed = newTitle.trim();
    if (trimmed.isEmpty) return;
    final success = await _chatService.renameTopic(topic.id, trimmed);
    if (success) {
      setState(() {
        topic.title = trimmed;
        _renamingId = null;
      });
    } else {
      _showSnack('Gagal mengganti judul.');
    }
  }

  void _toggleSidebar() {
    setState(() => _sidebarOpen = !_sidebarOpen);
    _sidebarOpen ? _sidebarCtrl.forward() : _sidebarCtrl.reverse();
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollCtrl.hasClients) {
        _scrollCtrl.animateTo(
          _scrollCtrl.position.maxScrollExtent,
          duration: const Duration(milliseconds: 300),
          curve: Curves.easeOut,
        );
      }
    });
  }

  void _scrollToMessage(int detailId) {
    final key = _messageKeys[detailId];
    if (key?.currentContext != null) {
      Scrollable.ensureVisible(
        key!.currentContext!,
        duration: const Duration(milliseconds: 400),
        curve: Curves.easeOut,
        alignment: 0.05,
      );
    }
    setState(() => _questionNavOpen = false);
  }

  void _syncMessageKeys() {
    final currentIds = _messages.map((m) => m.id).toSet();
    _messageKeys.removeWhere((id, _) => !currentIds.contains(id));
    for (final msg in _messages) {
      _messageKeys.putIfAbsent(msg.id, () => GlobalKey());
    }
  }

  void _showSnack(String msg) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(msg, style: GoogleFonts.nunitoSans(fontSize: 13, color: Colors.white)),
        backgroundColor: BatikColors.sogaGelap,
        behavior: SnackBarBehavior.floating,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
      ),
    );
  }

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    final int? pendingDetailId = _trackers.isNotEmpty
        ? _trackers.keys.first
        : null;
    final isMobile = MediaQuery.of(context).size.width < 768;

    final mainContent = Column(
      children: [
        _ChatTopBar(
          sidebarOpen: _sidebarOpen,
          onToggleSidebar: _toggleSidebar,
          title: _activeChatId != null
              ? _topics
                    .firstWhere(
                      (t) => t.id == _activeChatId,
                      orElse: () =>
                          ChatTopic(id: 0, title: 'Chat', createdAt: ''),
                    )
                    .title
              : 'Chat Baru',
          hasPending: _trackers.isNotEmpty,
        ),
        Expanded(child: _buildBody()),
        InputBar(
          key: _inputBarKey,  // ✅ Tambahkan key
          controller: _inputCtrl,
          focusNode: _inputFocus,
          sending: _sending,
          onSend: ({Uint8List? imageBytes, String? imageName}) => _sendMessage(
            imageBytes: imageBytes,
            imageName: imageName,
          ),
          onUploadPdfs: _uploadPdfs,
          onSetModel: (mode, {path}) => _chatService.setModel(mode, path: path),
          onGetModels: () => _chatService.getLocalModels(),
          onGetRagMode: () => _chatService.getRagMode(),
          onSetRagMode: (mode) => _chatService.setRagMode(mode),
          onGetActiveModel: () => _chatService.getCurrentModel(),
          pendingDetailId: pendingDetailId,
          onStop: pendingDetailId != null ? () => _stopGeneration(pendingDetailId) : null,
        ),
      ],
    );

    final sidebarWidget = ChatSidebar(
      topics: _topics,
      loading: _loadingTopics,
      activeChatId: _activeChatId,
      profile: _profile,
      renamingId: _renamingId,
      renamingTemp: _renamingTemp,
      onNewChat: _newChat,
      onSelectTopic: _selectTopic,
      onDeleteTopic: _deleteTopic,
      onStartRename: (t) => setState(() {
        _renamingId = t.id;
        _renamingTemp = t.title;
      }),
      onConfirmRename: (t, v) => _renameTopic(t, v),
      onCancelRename: () => setState(() => _renamingId = null),
      onRenameChange: (v) => setState(() => _renamingTemp = v),
      onProfileTap: () => context.push('/user_profile'),
      onLogout: _logout,
    );

    return Scaffold(
      backgroundColor: BatikColors.kertas,
      body: isMobile
          ? Stack(
              children: [
                mainContent,
                if (_sidebarOpen)
                  GestureDetector(
                    onTap: _toggleSidebar,
                    child: Container(color: Colors.black54),
                  ),
                AnimatedBuilder(
                  animation: _sidebarAnim,
                  builder: (context, child) {
                    return Transform.translate(
                      offset: Offset(
                        kSidebarWidth * (_sidebarAnim.value - 1),
                        0,
                      ),
                      child: child,
                    );
                  },
                  child: SizedBox(
                    width: kSidebarWidth,
                    height: double.infinity,
                    child: sidebarWidget,
                  ),
                ),
              ],
            )
          : Row(
              children: [
                SizeTransition(
                  sizeFactor: _sidebarAnim,
                  axis: Axis.horizontal,
                  child: sidebarWidget,
                ),
                Expanded(child: mainContent),
              ],
            ),
    );
  }

  Widget _buildBody() {
    if (_loadingMessages) {
      return const Center(
        child: CircularProgressIndicator(
          color: BatikColors.marun,
          strokeWidth: 2,
        ),
      );
    }
    if (_activeChatId == null &&
        _messages.isEmpty &&
        _pendingQuestion == null) {
      return _GreetingView(greeting: _greeting ?? _kGreetings[0]);
    }
    if (_messages.isEmpty && _pendingQuestion == null) {
      return const _GreetingView(
        greeting: 'Topik ini masih kosong. Mulai percakapan! 💬',
      );
    }

    final itemCount = _messages.length + (_pendingQuestion != null ? 1 : 0);

    return Stack(
      children: [
        ListView.builder(
          controller: _scrollCtrl,
          padding: const EdgeInsets.fromLTRB(24, 24, 24, 8),
          itemCount: itemCount,
          itemBuilder: (_, i) {
            if (_pendingQuestion != null && i == _messages.length) {
              return PendingBubble(
                question: _pendingQuestion!,
                imageBytes: _pendingImageBytes,
              );
            }

            final msg = _messages[i];
            final itemKey = _messageKeys.putIfAbsent(msg.id, () => GlobalKey());

            if (msg.isPending) {
              return KeyedSubtree(
                key: itemKey,
                child: PendingBubble(
                  question: msg.question,
                  imageBytes: msg.localImageBytes,
                ),
              );
            }

            if (msg.isDisconnected) {
              return KeyedSubtree(
                key: itemKey,
                child: DisconnectedBubble(
                  message: msg,
                  onResend: () => _resendMessage(msg),
                ),
              );
            }

            if (msg.isStopped) {
              return KeyedSubtree(
                key: itemKey,
                child: ValueListenableBuilder<int?>(
                  valueListenable: _chatService.playingTtsId,
                  builder: (context, playingId, _) {
                    final isPlaying = playingId == msg.id;
                    return StoppedBubble(
                      response: msg.response,
                      onRegenerate: () => _regenerateResponse(msg),
                      onCopyAnswer: () => _copyText(msg.response),
                      isPlayingTts: isPlaying,
                      onToggleTTS: () {
                        if (isPlaying) {
                          _stopTTS();
                        } else {
                          _playTTS(msg);
                        }
                      },
                    );
                  },
                ),
              );
            }

            return KeyedSubtree(
              key: itemKey,
              child: ValueListenableBuilder<int?>(
                valueListenable: _chatService.playingTtsId,
                builder: (context, playingId, _) {
                  final isPlaying = playingId == msg.id;
                  return MessagePair(
                    message: msg,
                    onEdit: (newQ) => _editMessage(msg, newQ),
                    onRegenerate: () => _regenerateResponse(msg),
                    onCopyQuestion: () => _copyText(msg.question),
                    onCopyAnswer: () => _copyText(msg.response),
                    isPlayingTts: isPlaying,
                    onToggleTTS: () {
                      if (isPlaying) {
                        _stopTTS();
                      } else {
                        _playTTS(msg);
                      }
                    },
                  );
                },
              ),
            );
          },
        ),

        // ── Question Navigator ──────────────────────────────────────────────
        if (_messages.isNotEmpty)
          _QuestionNavigator(
            messages: _messages,
            isOpen: _questionNavOpen,
            onToggle: () =>
                setState(() => _questionNavOpen = !_questionNavOpen),
            onSelect: _scrollToMessage,
          ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Question Navigator
// ---------------------------------------------------------------------------

class _QuestionNavigator extends StatefulWidget {
  const _QuestionNavigator({
    required this.messages,
    required this.isOpen,
    required this.onToggle,
    required this.onSelect,
  });

  final List<ChatMessage> messages;
  final bool isOpen;
  final VoidCallback onToggle;
  final void Function(int detailId) onSelect;

  @override
  State<_QuestionNavigator> createState() => _QuestionNavigatorState();
}

class _QuestionNavigatorState extends State<_QuestionNavigator>
    with SingleTickerProviderStateMixin {
  late final AnimationController _animCtrl;
  late final Animation<double> _panelAnim;

  // Pagination
  static const int _kPageSize = 5;
  int _page = 0;

  bool get _panelVisible => widget.isOpen;

  List<ChatMessage> get _visibleMessages =>
      widget.messages.where((m) => m.question.isNotEmpty).toList();

  int get _totalPages =>
      (_visibleMessages.length / _kPageSize).ceil().clamp(1, 999);

  List<ChatMessage> get _currentPageMessages {
    final all = _visibleMessages;
    final start = _page * _kPageSize;
    final end = (start + _kPageSize).clamp(0, all.length);
    return all.sublist(start, end);
  }

  @override
  void initState() {
    super.initState();
    _animCtrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 200),
    );
    _panelAnim = CurvedAnimation(parent: _animCtrl, curve: Curves.easeOut);
  }

  @override
  void didUpdateWidget(_QuestionNavigator old) {
    super.didUpdateWidget(old);
    if (widget.messages.length != old.messages.length) {
      final newTotal = (_visibleMessages.length / _kPageSize).ceil().clamp(
        1,
        999,
      );
      if (_page >= newTotal) _page = newTotal - 1;
    }
    if (widget.isOpen != old.isOpen) {
      widget.isOpen ? _animCtrl.forward() : _animCtrl.reverse();
    }
  }

  @override
  void dispose() {
    _animCtrl.dispose();
    super.dispose();
  }

  void _prevPage() {
    if (_page > 0) setState(() => _page--);
  }

  void _nextPage() {
    if (_page < _totalPages - 1) setState(() => _page++);
  }

  Widget _buildItem(int i) {
    final msg = _currentPageMessages[i];
    final globalIdx = _page * _kPageSize + i;
    final preview = msg.question.length > 38
        ? '${msg.question.substring(0, 38)}...'
        : msg.question;
    return InkWell(
      onTap: () => widget.onSelect(msg.id),
      borderRadius: BorderRadius.circular(6),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 20,
              child: Text(
                '${globalIdx + 1}.',
                style: GoogleFonts.nunitoSans(
                  fontSize: 11,
                  color: BatikColors.marun,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
            const SizedBox(width: 4),
            Expanded(
              child: Text(
                preview,
                style: GoogleFonts.nunitoSans(
                  fontSize: 11,
                  color: BatikColors.sogaGelap,
                  height: 1.4,
                ),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
              ),
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Positioned(
      left: 0,
      top: 0,
      right: 0,
      bottom: 0,
      child: Stack(
        children: [
          if (_panelVisible)
            Positioned.fill(
              child: GestureDetector(
                onTap: widget.onToggle,
                behavior: HitTestBehavior.translucent,
              ),
            ),
          Positioned(
            left: 0,
            top: 0,
            bottom: 0,
            child: Row(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.center,
              children: [
                GestureDetector(
                  onTap: widget.onToggle,
                  child: AnimatedContainer(
                    duration: const Duration(milliseconds: 200),
                    width: 16,
                    height: 72,
                    decoration: BoxDecoration(
                      color: _panelVisible
                          ? BatikColors.marun.withValues(alpha: 0.15)
                          : BatikColors.krem,
                      borderRadius: const BorderRadius.only(
                        topRight: Radius.circular(8),
                        bottomRight: Radius.circular(8),
                      ),
                      border: Border(
                        top: BorderSide(
                          color: _panelVisible
                              ? BatikColors.marun.withValues(alpha: 0.4)
                              : BatikColors.garis,
                        ),
                        right: BorderSide(
                          color: _panelVisible
                              ? BatikColors.marun.withValues(alpha: 0.4)
                              : BatikColors.garis,
                        ),
                        bottom: BorderSide(
                          color: _panelVisible
                              ? BatikColors.marun.withValues(alpha: 0.4)
                              : BatikColors.garis,
                        ),
                      ),
                    ),
                    child: Center(
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: List.generate(
                          3,
                          (i) => Container(
                            width: 3,
                            height: 3,
                            margin: const EdgeInsets.symmetric(vertical: 2),
                            decoration: BoxDecoration(
                              color: _panelVisible
                                  ? BatikColors.marun
                                  : BatikColors.abuTeks,
                              shape: BoxShape.circle,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ),

                SizeTransition(
                  sizeFactor: _panelAnim,
                  axis: Axis.horizontal,
                  child: FadeTransition(
                    opacity: _panelAnim,
                    child: Container(
                      width: 200,
                      height: 150,
                      margin: const EdgeInsets.only(left: 4),
                      decoration: BoxDecoration(
                        color: BatikColors.putih,
                        borderRadius: BorderRadius.circular(12),
                        border: Border.all(color: BatikColors.garis),
                        boxShadow: [
                          BoxShadow(
                            color: BatikColors.sogaGelap.withValues(alpha: 0.15),
                            blurRadius: 12,
                            offset: const Offset(2, 4),
                          ),
                        ],
                      ),
                      child: Column(
                        children: [
                          Padding(
                            padding: const EdgeInsets.fromLTRB(12, 10, 8, 6),
                            child: Row(
                              children: [
                                const Icon(
                                  Icons.format_list_bulleted_rounded,
                                  size: 13,
                                  color: BatikColors.marun,
                                ),
                                const SizedBox(width: 6),
                                Text(
                                  'Daftar Pertanyaan',
                                  style: GoogleFonts.nunitoSans(
                                    fontSize: 11,
                                    fontWeight: FontWeight.w600,
                                    color: BatikColors.marun,
                                  ),
                                ),
                                const Spacer(),
                                if (_totalPages > 1)
                                  Text(
                                    '${_page + 1}/$_totalPages',
                                    style: GoogleFonts.nunitoSans(
                                      fontSize: 10,
                                      color: BatikColors.abuTeks,
                                    ),
                                  ),
                              ],
                            ),
                          ),
                          const Divider(height: 1, color: BatikColors.garis),

                          if (_visibleMessages.length > 3)
                            Expanded(
                              child: Scrollbar(
                                thumbVisibility: true,
                                child: ListView.builder(
                                  padding: const EdgeInsets.symmetric(
                                    vertical: 4,
                                  ),
                                  itemCount: _currentPageMessages.length,
                                  itemBuilder: (_, i) => _buildItem(i),
                                ),
                              ),
                            )
                          else
                            ListView.builder(
                              shrinkWrap: true,
                              padding: const EdgeInsets.symmetric(vertical: 4),
                              itemCount: _currentPageMessages.length,
                              itemBuilder: (_, i) => _buildItem(i),
                            ),

                          if (_totalPages > 1) ...[
                            const Divider(height: 1, color: BatikColors.garis),
                            Padding(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 8,
                                vertical: 6,
                              ),
                              child: Row(
                                mainAxisAlignment:
                                    MainAxisAlignment.spaceBetween,
                                children: [
                                  _NavButton(
                                    icon: Icons.chevron_left_rounded,
                                    onTap: _page > 0 ? _prevPage : null,
                                  ),
                                  Text(
                                    '${_page * _kPageSize + 1}–'
                                    '${_page * _kPageSize + _currentPageMessages.length}'
                                    ' / ${_visibleMessages.length}',
                                    style: GoogleFonts.nunitoSans(
                                      fontSize: 10,
                                      color: BatikColors.abuTeks,
                                    ),
                                  ),
                                  _NavButton(
                                    icon: Icons.chevron_right_rounded,
                                    onTap: _page < _totalPages - 1
                                        ? _nextPage
                                        : null,
                                  ),
                                ],
                              ),
                            ),
                          ],
                        ],
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _NavButton extends StatelessWidget {
  const _NavButton({required this.icon, this.onTap});
  final IconData icon;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final enabled = onTap != null;
    return GestureDetector(
      onTap: onTap,
      child: Container(
        width: 26,
        height: 26,
        decoration: BoxDecoration(
          color: enabled ? BatikColors.krem : BatikColors.kertas,
          borderRadius: BorderRadius.circular(6),
          border: Border.all(
            color: enabled ? BatikColors.garis : BatikColors.garis,
          ),
        ),
        child: Icon(
          icon,
          size: 16,
          color: enabled ? BatikColors.soga : BatikColors.abuTeks.withValues(alpha: 0.4),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Chat Top Bar
// ---------------------------------------------------------------------------

class _ChatTopBar extends StatelessWidget {
  const _ChatTopBar({
    required this.sidebarOpen,
    required this.onToggleSidebar,
    required this.title,
    required this.hasPending,
  });

  final bool sidebarOpen;
  final VoidCallback onToggleSidebar;
  final String title;
  final bool hasPending;

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 56,
      padding: const EdgeInsets.symmetric(horizontal: 12),
      decoration: const BoxDecoration(
        color: BatikColors.marun,
        border: Border(bottom: BorderSide(color: BatikColors.marunGelap)),
      ),
      child: Row(
        children: [
          Tooltip(
            message: sidebarOpen ? 'Sembunyikan Sidebar' : 'Tampilkan Sidebar',
            child: InkWell(
              onTap: onToggleSidebar,
              borderRadius: BorderRadius.circular(8),
              child: Container(
                padding: const EdgeInsets.all(7),
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: Colors.white24),
                ),
                child: Icon(
                  sidebarOpen ? Icons.menu_open_rounded : Icons.menu_rounded,
                  size: 18,
                  color: Colors.white,
                ),
              ),
            ),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Text(
              title,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: GoogleFonts.nunitoSans(
                fontSize: 14,
                fontWeight: FontWeight.w600,
                color: Colors.white,
              ),
            ),
          ),
          if (hasPending) ...[
            const SizedBox(width: 8),
            Tooltip(
              message: 'Menunggu respons AI (pipeline aktif)...',
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  _PulsingDot(),
                  const SizedBox(width: 5),
                  Text(
                    'Memproses',
                    style: GoogleFonts.nunitoSans(
                      fontSize: 11,
                      color: Colors.white,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _PulsingDot extends StatefulWidget {
  @override
  State<_PulsingDot> createState() => _PulsingDotState();
}

class _PulsingDotState extends State<_PulsingDot>
    with SingleTickerProviderStateMixin {
  late final AnimationController _ctrl;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 800),
    )..repeat(reverse: true);
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => FadeTransition(
    opacity: _ctrl,
    child: Container(
      width: 7,
      height: 7,
      decoration: const BoxDecoration(
        color: BatikColors.emas,
        shape: BoxShape.circle,
      ),
    ),
  );
}

// ---------------------------------------------------------------------------
// Greeting View
// ---------------------------------------------------------------------------

class _GreetingView extends StatelessWidget {
  const _GreetingView({required this.greeting});
  final String greeting;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 40),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 64,
              height: 64,
              decoration: BoxDecoration(
                color: const Color(0x1A8B251E),
                shape: BoxShape.circle,
                border: Border.all(
                  color: BatikColors.marun.withValues(alpha: 0.4),
                  width: 1.5,
                ),
              ),
              child: const Icon(
                Icons.account_tree_rounded, // Icon Graph RAG
                color: BatikColors.marun,
                size: 30,
              ),
            ),
            const SizedBox(height: 20),
            Text(
              greeting,
              textAlign: TextAlign.center,
              style: GoogleFonts.playfairDisplay(
                fontSize: 18,
                fontWeight: FontWeight.w600,
                color: BatikColors.sogaGelap,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 10),
            Text(
              'Ketik pertanyaan Anda di bawah untuk memulai percakapan dengan Ragna.',
              textAlign: TextAlign.center,
              style: GoogleFonts.nunitoSans(
                fontSize: 13,
                color: BatikColors.abuTeks,
              ),
            ),
          ],
        ),
      ),
    );
  }
}