// lib/chats/input_bar.dart
import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:frontend/chats/service_chats.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:frontend/chats/batik_theme.dart';
import 'package:file_picker/file_picker.dart';
import 'package:frontend/services/token_storage.dart';
import 'package:speech_to_text/speech_to_text.dart' as stt;
import 'package:image_picker/image_picker.dart';

// ---------------------------------------------------------------------------
// Intents untuk Shortcuts
// ---------------------------------------------------------------------------

class SendMessageIntent extends Intent {
  const SendMessageIntent();
}

// ---------------------------------------------------------------------------
// Input Bar
// ---------------------------------------------------------------------------

class InputBar extends StatefulWidget {
  const InputBar({
    super.key,
    required this.controller,
    required this.focusNode,
    required this.sending,
    required this.onSend,
    required this.onUploadPdfs,
    required this.onSetModel,
    required this.onGetModels,
    required this.onSetRagMode,
    required this.onGetRagMode,
    required this.onGetActiveModel,  // <-- TAMBAHKAN (required)
    this.pendingDetailId,
    this.onStop,
  });

  final TextEditingController controller;
  final FocusNode focusNode;
  final bool sending;
  final void Function({Uint8List? imageBytes, String? imageName}) onSend;
  final Future<void> Function(
    List<PdfUploadFile> files, {
    void Function(int done, int total)? onProgress,
  }) onUploadPdfs;
  final Future<bool> Function(String mode, {String? path}) onSetModel;
  final Future<List<Map<String, dynamic>>> Function() onGetModels;
  final Future<bool> Function(String mode) onSetRagMode;
  final Future<String> Function() onGetRagMode;
  final Future<String?> Function() onGetActiveModel;  // <-- TAMBAHKAN
  final int? pendingDetailId;
  final VoidCallback? onStop;

  @override
  State<InputBar> createState() => _InputBarState();
}

class _InputBarState extends State<InputBar> {
  bool _hasText = false;
  final stt.SpeechToText _speechToText = stt.SpeechToText();
  bool _isListening = false;
  bool _speechEnabled = false;

  String _activeModelId = '';
  bool _modelSwitching = false;

  String _ragMode = 'improved';  
  bool _ragModeSwitching = false;

  // Image Picker State
  Uint8List? _selectedImageBytes;
  String? _selectedImageName;
  final ImagePicker _picker = ImagePicker();

  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_onTextChanged);
    _initSpeech();
  }

  void initializeAfterAuth() {
    _initRagMode();
    _initActiveModel();
  }

  void _onTextChanged() {
    final has = widget.controller.text.trim().isNotEmpty;
    if (has != _hasText && mounted) {
      setState(() => _hasText = has);
    }
  }

  @override
  void dispose() {
    widget.controller.removeListener(_onTextChanged);
    _speechToText.stop();
    super.dispose();
  }

  // lib/chats/inputbar.dart - UPDATE _initActiveModel

  Future<void> _initActiveModel() async {
    try {
      // Cek apakah token tersedia sebelum request
      final token = await TokenStorage.read(key: 'access_token');
      if (token == null || token.isEmpty) {
        debugPrint('⚠️ No token available, skipping active model fetch');
        return;
      }
      
      final modelId = await widget.onGetActiveModel();
      if (modelId != null && mounted) {
        setState(() => _activeModelId = modelId);
      } else if (mounted) {
        setState(() => _activeModelId = '');
      }
    } catch (e) {
      debugPrint('Error loading active model: $e');
      if (mounted) {
        setState(() => _activeModelId = '');
      }
    }
  }

  Future<void> _initRagMode() async {
    try {
      // Cek apakah token tersedia sebelum request
      final token = await TokenStorage.read(key: 'access_token');
      if (token == null || token.isEmpty) {
        debugPrint('⚠️ No token available, skipping RAG mode fetch');
        return;
      }
      
      final mode = await widget.onGetRagMode();
      if (mounted) {
        setState(() => _ragMode = mode);
      }
    } catch (e) {
      debugPrint('Error loading RAG mode: $e');
    }
  }

  void _initSpeech() async {
    try {
      _speechEnabled = await _speechToText.initialize(
        onStatus: (status) {
          if ((status == 'done' || status == 'notListening') && mounted) {
            setState(() => _isListening = false);
          }
        },
        onError: (error) {
          if (mounted) setState(() => _isListening = false);
        },
      );
      if (mounted) setState(() {});
    } catch (e) {
      debugPrint('STT Error: $e');
    }
  }

  void _startListening() async {
    await _speechToText.listen(
      onResult: (result) {
        if (mounted) {
          setState(() {
            widget.controller.text = result.recognizedWords;
            widget.controller.selection = TextSelection.collapsed(
              offset: widget.controller.text.length,
            );
          });
        }
      },
      localeId: 'id_ID',
    );
    if (mounted) setState(() => _isListening = true);
  }

  void _stopListening() async {
    await _speechToText.stop();
    if (mounted) setState(() => _isListening = false);
  }

  // Pick Image Logic
  Future<void> _pickImage() async {
    try {
      final XFile? image = await _picker.pickImage(source: ImageSource.gallery);
      if (image != null) {
        final bytes = await image.readAsBytes();
        setState(() {
          _selectedImageBytes = bytes;
          _selectedImageName = image.name;
        });
      }
    } catch (e) {
      debugPrint('Image picker error: $e');
    }
  }

  void _triggerSend() {
    widget.onSend(
      imageBytes: _selectedImageBytes, 
      imageName: _selectedImageName
    );
    setState(() {
      _selectedImageBytes = null;
      _selectedImageName = null;
    });
  }

  // ── Dialog RAG Mode ────────────────────────────────────────
  void _showRagModeDialog(BuildContext context) {
    final isMobile = MediaQuery.of(context).size.width < 600;
    String selectedMode = _ragMode;

    showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setDS) => Dialog(
          backgroundColor: BatikColors.putih,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(16),
            side: const BorderSide(color: BatikColors.garis),
          ),
          child: Container(
            width: isMobile ? double.infinity : 360,
            padding: EdgeInsets.all(isMobile ? 20 : 24),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Container(
                      width: 36,
                      height: 36,
                      decoration: BoxDecoration(
                        color: BatikColors.marun.withValues(alpha: 0.12),
                        borderRadius: BorderRadius.circular(10),
                      ),
                      child: const Icon(
                        Icons.tune_rounded,
                        color: BatikColors.marun,
                        size: 18,
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Text(
                        'Mode RAG',
                        style: GoogleFonts.nunitoSans(
                          fontSize: 15,
                          fontWeight: FontWeight.w600,
                          color: BatikColors.sogaGelap,
                        ),
                      ),
                    ),
                    GestureDetector(
                      onTap: () => Navigator.of(ctx).pop(),
                      child: const Icon(
                        Icons.close_rounded,
                        color: BatikColors.abuTeks,
                        size: 20,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  'Pilih metode retrieval untuk menjawab pertanyaan teknis',
                  style: GoogleFonts.nunitoSans(
                    fontSize: 11,
                    color: BatikColors.abuTeks,
                  ),
                ),
                const SizedBox(height: 16),
                
                _buildRagModeOption(
                  mode: 'improved',
                  title: 'Improved RAG',
                  description: 'Lebih akurat untuk pertanyaan kompleks',
                  icon: Icons.auto_awesome_rounded,
                  selectedMode: selectedMode,
                  onSelect: () => setDS(() => selectedMode = 'improved'),
                ),
                const SizedBox(height: 8),
                
                _buildRagModeOption(
                  mode: 'regular',
                  title: 'Regular RAG',
                  description: 'Lebih cepat, cocok untuk pertanyaan langsung',
                  icon: Icons.speed_rounded,
                  selectedMode: selectedMode,
                  onSelect: () => setDS(() => selectedMode = 'regular'),
                ),
                
                const SizedBox(height: 20),
                Row(
                  children: [
                    Expanded(
                      child: TextButton(
                        onPressed: () => Navigator.of(ctx).pop(),
                        style: TextButton.styleFrom(
                          padding: const EdgeInsets.symmetric(vertical: 12),
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(10),
                          ),
                        ),
                        child: Text(
                          'Batal',
                          style: GoogleFonts.nunitoSans(
                            fontSize: 13,
                            color: BatikColors.abuTeks,
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: ElevatedButton(
                        onPressed: selectedMode == _ragMode
                            ? null
                            : () async {
                                Navigator.of(ctx).pop();
                                if (mounted) {
                                  setState(() => _ragModeSwitching = true);
                                }
                                try {
                                  final success = await widget.onSetRagMode(selectedMode);
                                  if (mounted && success) {
                                    setState(() {
                                      _ragMode = selectedMode;
                                      _ragModeSwitching = false;
                                    });
                                    ScaffoldMessenger.of(context).showSnackBar(
                                      SnackBar(
                                        content: Text(
                                          selectedMode == 'improved'
                                              ? 'Mode RAG: Improved (Neo4j + context)'
                                              : 'Mode RAG: Regular (Simple retrieval)',
                                        ),
                                        backgroundColor: BatikColors.hijau,
                                        duration: const Duration(seconds: 2),
                                      ),
                                    );
                                  } else if (mounted) {
                                    setState(() => _ragModeSwitching = false);
                                    ScaffoldMessenger.of(context).showSnackBar(
                                      const SnackBar(
                                        content: Text('Gagal mengganti mode RAG'),
                                        backgroundColor: BatikColors.marun,
                                      ),
                                    );
                                  }
                                } catch (e) {
                                  if (mounted) {
                                    setState(() => _ragModeSwitching = false);
                                    ScaffoldMessenger.of(context).showSnackBar(
                                      SnackBar(
                                        content: Text('Error: $e'),
                                        backgroundColor: BatikColors.marun,
                                      ),
                                    );
                                  }
                                }
                              },
                        style: ElevatedButton.styleFrom(
                          backgroundColor: BatikColors.marun,
                          disabledBackgroundColor: BatikColors.krem,
                          padding: const EdgeInsets.symmetric(vertical: 12),
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(10),
                          ),
                          elevation: 0,
                        ),
                        child: Text(
                          'Terapkan',
                          style: GoogleFonts.nunitoSans(
                            fontSize: 13,
                            fontWeight: FontWeight.w600,
                            color: selectedMode == _ragMode
                                ? BatikColors.abuTeks
                                : Colors.white,
                          ),
                        ),
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildRagModeOption({
    required String mode,
    required String title,
    required String description,
    required IconData icon,
    required String selectedMode,
    required VoidCallback onSelect,
  }) {
    final isSelected = selectedMode == mode;
    final Color accent = mode == 'improved' 
        ? BatikColors.marun 
        : BatikColors.sogaMuda;

    return InkWell(
      borderRadius: BorderRadius.circular(12),
      onTap: onSelect,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 150),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        decoration: BoxDecoration(
          color: isSelected ? accent.withValues(alpha: 0.08) : BatikColors.kertas,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(
            color: isSelected ? accent.withValues(alpha: 0.5) : BatikColors.garis,
            width: isSelected ? 1.5 : 1,
          ),
        ),
        child: Row(
          children: [
            Container(
              width: 40,
              height: 40,
              decoration: BoxDecoration(
                color: isSelected ? accent.withValues(alpha: 0.15) : BatikColors.krem,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Icon(
                icon,
                size: 20,
                color: isSelected ? accent : BatikColors.abuTeks,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    title,
                    style: GoogleFonts.nunitoSans(
                      fontSize: 13,
                      fontWeight: FontWeight.w600,
                      color: isSelected ? BatikColors.sogaGelap : BatikColors.abuTeks,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    description,
                    style: GoogleFonts.nunitoSans(
                      fontSize: 10.5,
                      color: BatikColors.abuTeks,
                    ),
                  ),
                ],
              ),
            ),
            if (isSelected)
              Container(
                width: 18,
                height: 18,
                decoration: BoxDecoration(
                  color: accent,
                  shape: BoxShape.circle,
                ),
                child: const Icon(
                  Icons.check_rounded,
                  size: 12,
                  color: Colors.white,
                ),
              ),
          ],
        ),
      ),
    );
  }

  // ── Model Selector Dialog ─────────────────────────────────────────────────

  Future<void> _showModelDialog(BuildContext context) async {
    List<Map<String, dynamic>> groqModels = [];
    bool loadingModels = true;
    String selectedId = _activeModelId;
    final isMobile = MediaQuery.of(context).size.width < 600;

    await showDialog(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setDS) {
          if (loadingModels) {
            Future.microtask(() async {
              try {
                final raw = await widget.onGetModels();
                if (ctx.mounted) {
                  setDS(() {
                    groqModels = raw.map((m) => Map<String, dynamic>.from(m)).toList();
                    loadingModels = false;
                  });
                }
              } catch (_) {
                if (ctx.mounted) setDS(() => loadingModels = false);
              }
            });
          }

          return Dialog(
            backgroundColor: BatikColors.putih,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(16),
              side: const BorderSide(color: BatikColors.garis),
            ),
            child: Container(
              width: isMobile ? double.infinity : 420,
              constraints: BoxConstraints(
                maxWidth: 500,
                maxHeight: isMobile ? MediaQuery.of(ctx).size.height * 0.9 : 620,
              ),
              child: Padding(
                padding: EdgeInsets.all(isMobile ? 16 : 24),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Container(
                          width: 36,
                          height: 36,
                          decoration: BoxDecoration(
                            color: BatikColors.marun.withValues(alpha: 0.12),
                            borderRadius: BorderRadius.circular(10),
                          ),
                          child: const Icon(
                            Icons.auto_awesome_rounded,
                            color: BatikColors.marun,
                            size: 18,
                          ),
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                'Pilih Model',
                                style: GoogleFonts.nunitoSans(
                                  fontSize: isMobile ? 14 : 15,
                                  fontWeight: FontWeight.w600,
                                  color: BatikColors.sogaGelap,
                                ),
                              ),
                              Text(
                                'Semua model berjalan via Groq API',
                                style: GoogleFonts.nunitoSans(
                                  fontSize: 10.5,
                                  color: BatikColors.abuTeks,
                                ),
                              ),
                            ],
                          ),
                        ),
                        GestureDetector(
                          onTap: () => Navigator.of(ctx).pop(),
                          child: const Icon(
                            Icons.close_rounded,
                            color: BatikColors.abuTeks,
                            size: 20,
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    Expanded(
                      child: loadingModels
                          ? const Center(
                              child: CircularProgressIndicator(
                                color: BatikColors.marun,
                                strokeWidth: 2,
                              ),
                            )
                          : groqModels.isEmpty
                          ? Center(
                              child: Text(
                                'Gagal memuat daftar model.',
                                style: GoogleFonts.nunitoSans(
                                  fontSize: 12,
                                  color: BatikColors.abuTeks,
                                ),
                              ),
                            )
                          : ListView.separated(
                              shrinkWrap: true,
                              physics: const BouncingScrollPhysics(),
                              itemCount: groqModels.length,
                              separatorBuilder: (_, _) => const SizedBox(height: 8),
                              itemBuilder: (_, i) {
                                final m = groqModels[i];
                                final id = m['id'] as String;
                                final name = m['name'] as String;
                                final provider = m['provider'] as String;
                                final desc = m['description'] as String? ?? '';
                                final tier = m['tier'] as String? ?? 'large';
                                final isSelected = selectedId == id;

                                final Color accent = id.startsWith('mistral')
                                    ? BatikColors.nila
                                    : id.startsWith('qwen')
                                    ? BatikColors.sogaMuda
                                    : BatikColors.marun;

                                final String tierLabel = tier == 'small'
                                    ? 'Ringan'
                                    : tier == 'medium'
                                    ? 'Sedang'
                                    : 'Kuat';
                                final Color tierColor = tier == 'small'
                                    ? BatikColors.hijau
                                    : tier == 'medium'
                                    ? BatikColors.sogaMuda
                                    : BatikColors.marunGelap;

                                return InkWell(
                                  borderRadius: BorderRadius.circular(12),
                                  onTap: () => setDS(() => selectedId = id),
                                  child: AnimatedContainer(
                                    duration: const Duration(milliseconds: 150),
                                    padding: const EdgeInsets.symmetric(
                                      horizontal: 14,
                                      vertical: 12,
                                    ),
                                    decoration: BoxDecoration(
                                      color: isSelected
                                          ? accent.withValues(alpha: 0.08)
                                          : BatikColors.kertas,
                                      borderRadius: BorderRadius.circular(12),
                                      border: Border.all(
                                        color: isSelected
                                            ? accent.withValues(alpha: 0.5)
                                            : BatikColors.garis,
                                        width: isSelected ? 1.5 : 1,
                                      ),
                                    ),
                                    child: Row(
                                      children: [
                                        Container(
                                          width: 34,
                                          height: 34,
                                          decoration: BoxDecoration(
                                            color: isSelected
                                                ? accent.withValues(alpha: 0.15)
                                                : BatikColors.krem,
                                            borderRadius: BorderRadius.circular(8),
                                          ),
                                          child: Icon(
                                            Icons.cloud_rounded,
                                            size: 17,
                                            color: isSelected
                                                ? accent
                                                : BatikColors.abuTeks,
                                          ),
                                        ),
                                        const SizedBox(width: 12),
                                        Expanded(
                                          child: Column(
                                            crossAxisAlignment: CrossAxisAlignment.start,
                                            children: [
                                              Row(
                                                children: [
                                                  Expanded(
                                                    child: Text(
                                                      name,
                                                      style: GoogleFonts.nunitoSans(
                                                        fontSize: 13,
                                                        fontWeight: FontWeight.w600,
                                                        color: isSelected
                                                            ? BatikColors.sogaGelap
                                                            : BatikColors.abuTeks,
                                                      ),
                                                    ),
                                                  ),
                                                  Container(
                                                    padding: const EdgeInsets.symmetric(
                                                      horizontal: 6,
                                                      vertical: 2,
                                                    ),
                                                    decoration: BoxDecoration(
                                                      color: tierColor.withValues(alpha: 0.12),
                                                      borderRadius: BorderRadius.circular(4),
                                                    ),
                                                    child: Text(
                                                      tierLabel,
                                                      style: GoogleFonts.nunitoSans(
                                                        fontSize: 9.5,
                                                        color: tierColor,
                                                        fontWeight: FontWeight.w600,
                                                      ),
                                                    ),
                                                  ),
                                                ],
                                              ),
                                              const SizedBox(height: 2),
                                              Text(
                                                provider,
                                                style: GoogleFonts.nunitoSans(
                                                  fontSize: 10.5,
                                                  color: BatikColors.abuTeks,
                                                ),
                                              ),
                                              if (desc.isNotEmpty) ...[
                                                const SizedBox(height: 3),
                                                Text(
                                                  desc,
                                                  style: GoogleFonts.nunitoSans(
                                                    fontSize: 10,
                                                    color: BatikColors.abuTeks,
                                                  ),
                                                  maxLines: 2,
                                                  overflow: TextOverflow.ellipsis,
                                                ),
                                              ],
                                            ],
                                          ),
                                        ),
                                        const SizedBox(width: 8),
                                        if (isSelected)
                                          Container(
                                            width: 18,
                                            height: 18,
                                            decoration: BoxDecoration(
                                              color: accent,
                                              shape: BoxShape.circle,
                                            ),
                                            child: const Icon(
                                              Icons.check_rounded,
                                              size: 12,
                                              color: Colors.white,
                                            ),
                                          ),
                                      ],
                                    ),
                                  ),
                                );
                              },
                            ),
                    ),
                    const SizedBox(height: 20),
                    Row(
                      children: [
                        Expanded(
                          child: TextButton(
                            onPressed: () => Navigator.of(ctx).pop(),
                            style: TextButton.styleFrom(
                              padding: const EdgeInsets.symmetric(vertical: 12),
                              shape: RoundedRectangleBorder(
                                borderRadius: BorderRadius.circular(10),
                              ),
                            ),
                            child: Text(
                              'Batal',
                              style: GoogleFonts.nunitoSans(
                                fontSize: 13,
                                color: BatikColors.abuTeks,
                              ),
                            ),
                          ),
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          child: ElevatedButton(
                            onPressed: selectedId == _activeModelId
                                ? null
                                : () async {
                                    Navigator.of(ctx).pop();
                                    if (mounted) {
                                      setState(() => _modelSwitching = true);
                                    }
                                    try {
                                      await widget.onSetModel(
                                        'groq',
                                        path: selectedId,
                                      );
                                      if (mounted) {
                                        setState(() {
                                          _activeModelId = selectedId;
                                          _modelSwitching = false;
                                        });
                                      }
                                    } catch (e) {
                                      if (mounted) {
                                        setState(() => _modelSwitching = false);
                                        ScaffoldMessenger.of(context).showSnackBar(
                                          SnackBar(
                                            content: Text('Gagal mengganti model: $e'),
                                            backgroundColor: BatikColors.marun,
                                          ),
                                        );
                                      }
                                    }
                                  },
                            style: ElevatedButton.styleFrom(
                              backgroundColor: BatikColors.marun,
                              disabledBackgroundColor: BatikColors.krem,
                              padding: const EdgeInsets.symmetric(vertical: 12),
                              shape: RoundedRectangleBorder(
                                borderRadius: BorderRadius.circular(10),
                              ),
                              elevation: 0,
                            ),
                            child: Text(
                              'Terapkan',
                              style: GoogleFonts.nunitoSans(
                                fontSize: 13,
                                fontWeight: FontWeight.w600,
                                color: selectedId == _activeModelId
                                    ? BatikColors.abuTeks
                                    : Colors.white,
                              ),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          );
        },
      ),
    );
  }

  void _showUploadDialog(BuildContext context) {
    List<PdfUploadFile> selectedFiles = [];
    bool isUploading = false;
    int uploadDone = 0;
    String embedderType = 'improved'; 

    showDialog(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setDialogState) {
          Future<void> pickFiles() async {
            final result = await FilePicker.platform.pickFiles(
              type: FileType.custom,
              allowedExtensions: ['pdf'],
              allowMultiple: true,
              withData: true,
            );
            if (result == null || result.files.isEmpty) return;

            final newFiles = result.files
                .where((f) => f.bytes != null)
                .map((f) => PdfUploadFile(
                      bytes: f.bytes!,
                      name: f.name,
                      embedderType: embedderType,
                    ))
                .toList();

            setDialogState(() {
              final existingNames = selectedFiles.map((f) => f.name).toSet();
              for (final f in newFiles) {
                if (!existingNames.contains(f.name)) {
                  selectedFiles.add(f);
                  existingNames.add(f.name);
                }
              }
            });
          }

          void removeFile(int index) {
            setDialogState(() => selectedFiles.removeAt(index));
          }

          Future<void> doUpload() async {
            if (selectedFiles.isEmpty) return;
            setDialogState(() {
              isUploading = true;
              uploadDone = 0;
            });

            final filesToUpload = selectedFiles
                .map((f) => PdfUploadFile(
                      bytes: f.bytes,
                      name: f.name,
                      judul: f.judul,
                      penulis: f.penulis,
                      tahun: f.tahun,
                      embedderType: embedderType,
                    ))
                .toList();

            await widget.onUploadPdfs(
              filesToUpload,
              onProgress: (done, total) {
                if (ctx.mounted) {
                  setDialogState(() => uploadDone = done);
                }
              },
            );

            if (ctx.mounted) Navigator.of(ctx).pop();
          }

          final hasFiles = selectedFiles.isNotEmpty;

          return Dialog(
            backgroundColor: BatikColors.putih,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(16),
              side: const BorderSide(color: BatikColors.garis),
            ),
            child: ConstrainedBox(
              constraints: BoxConstraints(
                maxWidth: 480,
                maxHeight: MediaQuery.of(ctx).size.height * 0.85,
              ),
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Column(
                  mainAxisSize: MainAxisSize.max,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Container(
                          width: 36,
                          height: 36,
                          decoration: BoxDecoration(
                            color: BatikColors.marun.withValues(alpha: 0.12),
                            borderRadius: BorderRadius.circular(10),
                          ),
                          child: const Icon(
                            Icons.auto_stories_rounded,
                            color: BatikColors.marun,
                            size: 18,
                          ),
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Text(
                            'Tambah Pengetahuan Bot',
                            style: GoogleFonts.nunitoSans(
                              fontSize: 15,
                              fontWeight: FontWeight.w600,
                              color: BatikColors.sogaGelap,
                            ),
                          ),
                        ),
                        if (!isUploading)
                          GestureDetector(
                            onTap: () => Navigator.of(ctx).pop(),
                            child: const Icon(
                              Icons.close_rounded,
                              color: BatikColors.abuTeks,
                              size: 20,
                            ),
                          ),
                      ],
                    ),
                    const SizedBox(height: 6),
                    Text(
                      'Pilih satu atau beberapa file PDF sekaligus',
                      style: GoogleFonts.nunitoSans(
                        fontSize: 12,
                        color: BatikColors.abuTeks,
                      ),
                    ),
                    const SizedBox(height: 12),

                    // ── Pilihan Embedder ──────────────────────────────────
                    Container(
                      decoration: BoxDecoration(
                        color: BatikColors.kertas,
                        borderRadius: BorderRadius.circular(12),
                        border: Border.all(color: BatikColors.garis),
                      ),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Padding(
                            padding: const EdgeInsets.fromLTRB(14, 10, 14, 6),
                            child: Row(
                              children: [
                                const Icon(
                                  Icons.tune_rounded,
                                  size: 14,
                                  color: BatikColors.marun,
                                ),
                                const SizedBox(width: 6),
                                Text(
                                  'Mode Embedder',
                                  style: GoogleFonts.nunitoSans(
                                    fontSize: 11.5,
                                    fontWeight: FontWeight.w600,
                                    color: BatikColors.marun,
                                  ),
                                ),
                              ],
                            ),
                          ),
                          const Divider(height: 1, color: BatikColors.garis),
                          // Improved option
                          InkWell(
                            onTap: isUploading
                                ? null
                                : () => setDialogState(
                                      () => embedderType = 'improved',
                                    ),
                            child: Padding(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 14,
                                vertical: 10,
                              ),
                              child: Row(
                                children: [
                                  AnimatedContainer(
                                    duration: const Duration(milliseconds: 150),
                                    width: 16,
                                    height: 16,
                                    decoration: BoxDecoration(
                                      shape: BoxShape.circle,
                                      border: Border.all(
                                        color: embedderType == 'improved'
                                            ? BatikColors.marun
                                            : BatikColors.abuTeks,
                                        width: 2,
                                      ),
                                      color: embedderType == 'improved'
                                          ? BatikColors.marun
                                          : Colors.transparent,
                                    ),
                                    child: embedderType == 'improved'
                                        ? const Icon(
                                            Icons.check,
                                            size: 10,
                                            color: Colors.white,
                                          )
                                        : null,
                                  ),
                                  const SizedBox(width: 10),
                                  Expanded(
                                    child: Column(
                                      crossAxisAlignment:
                                          CrossAxisAlignment.start,
                                      children: [
                                        Text(
                                          'Improved Embedder',
                                          style: GoogleFonts.nunitoSans(
                                            fontSize: 12,
                                            fontWeight: FontWeight.w600,
                                            color: embedderType == 'improved'
                                                ? BatikColors.sogaGelap
                                                : BatikColors.abuTeks,
                                          ),
                                        ),
                                        Text(
                                          'Akurasi lebih tinggi',
                                          style: GoogleFonts.nunitoSans(
                                            fontSize: 10,
                                            color: BatikColors.abuTeks,
                                          ),
                                        ),
                                      ],
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          ),
                          const Divider(height: 1, color: BatikColors.garis),
                          // Raw option
                          InkWell(
                            onTap: isUploading
                                ? null
                                : () => setDialogState(
                                      () => embedderType = 'raw',
                                    ),
                            child: Padding(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 14,
                                vertical: 10,
                              ),
                              child: Row(
                                children: [
                                  AnimatedContainer(
                                    duration: const Duration(milliseconds: 150),
                                    width: 16,
                                    height: 16,
                                    decoration: BoxDecoration(
                                      shape: BoxShape.circle,
                                      border: Border.all(
                                        color: embedderType == 'raw'
                                            ? BatikColors.sogaMuda
                                            : BatikColors.abuTeks,
                                        width: 2,
                                      ),
                                      color: embedderType == 'raw'
                                          ? BatikColors.sogaMuda
                                          : Colors.transparent,
                                    ),
                                    child: embedderType == 'raw'
                                        ? const Icon(
                                            Icons.check,
                                            size: 10,
                                            color: Colors.white,
                                          )
                                        : null,
                                  ),
                                  const SizedBox(width: 10),
                                  Expanded(
                                    child: Column(
                                      crossAxisAlignment:
                                          CrossAxisAlignment.start,
                                      children: [
                                        Text(
                                          'Raw Embedder',
                                          style: GoogleFonts.nunitoSans(
                                            fontSize: 12,
                                            fontWeight: FontWeight.w600,
                                            color: embedderType == 'raw'
                                                ? BatikColors.sogaGelap
                                                : BatikColors.abuTeks,
                                          ),
                                        ),
                                        Text(
                                          'Embed teks mentah langsung',
                                          style: GoogleFonts.nunitoSans(
                                            fontSize: 10,
                                            color: BatikColors.abuTeks,
                                          ),
                                        ),
                                      ],
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 12),
                    GestureDetector(
                      onTap: isUploading ? null : pickFiles,
                      child: Container(
                        width: double.infinity,
                        padding: const EdgeInsets.symmetric(
                          vertical: 20,
                          horizontal: 16,
                        ),
                        decoration: BoxDecoration(
                          color: BatikColors.kertas,
                          borderRadius: BorderRadius.circular(12),
                          border: Border.all(
                            color: hasFiles
                                ? BatikColors.marun.withValues(alpha: 0.4)
                                : BatikColors.garis,
                            width: hasFiles ? 1.5 : 1,
                          ),
                        ),
                        child: Column(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Icon(
                              hasFiles
                                  ? Icons.picture_as_pdf_rounded
                                  : Icons.upload_file_rounded,
                              color: hasFiles
                                  ? BatikColors.marun
                                  : BatikColors.abuTeks,
                              size: 32,
                            ),
                            const SizedBox(height: 8),
                            Text(
                              hasFiles
                                  ? 'Ketuk untuk menambah lebih banyak file'
                                  : 'Ketuk untuk memilih file PDF',
                              style: GoogleFonts.nunitoSans(
                                fontSize: 13,
                                fontWeight: FontWeight.w500,
                                color: hasFiles
                                    ? BatikColors.marun
                                    : BatikColors.abuTeks,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                    if (hasFiles) ...[
                      const SizedBox(height: 12),
                      if (isUploading) ...[
                        Padding(
                          padding: const EdgeInsets.only(bottom: 8),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                'Mengunggah $uploadDone dari ${selectedFiles.length} file...',
                                style: GoogleFonts.nunitoSans(
                                  fontSize: 12,
                                  color: BatikColors.marun,
                                ),
                              ),
                              const SizedBox(height: 6),
                              LinearProgressIndicator(
                                value: selectedFiles.isEmpty
                                    ? 0
                                    : uploadDone / selectedFiles.length,
                                backgroundColor: BatikColors.garis,
                                color: BatikColors.marun,
                                borderRadius: BorderRadius.circular(4),
                              ),
                            ],
                          ),
                        ),
                      ],
                      Flexible(
                        child: ListView.separated(
                          shrinkWrap: false,
                          itemCount: selectedFiles.length,
                          separatorBuilder: (_, _) => const SizedBox(height: 6),
                          itemBuilder: (_, i) {
                            final f = selectedFiles[i];
                            final isDone = isUploading && i < uploadDone;
                            return Container(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 12,
                                vertical: 8,
                              ),
                              decoration: BoxDecoration(
                                color: BatikColors.kertas,
                                borderRadius: BorderRadius.circular(8),
                                border: Border.all(
                                  color: isDone
                                      ? BatikColors.marun.withValues(alpha: 0.4)
                                      : BatikColors.garis,
                                ),
                              ),
                              child: Row(
                                children: [
                                  Icon(
                                    isDone
                                        ? Icons.check_circle_outline_rounded
                                        : Icons.picture_as_pdf_rounded,
                                    size: 16,
                                    color: isDone
                                        ? BatikColors.marun
                                        : BatikColors.abuTeks,
                                  ),
                                  const SizedBox(width: 8),
                                  Expanded(
                                    child: Text(
                                      f.name,
                                      style: GoogleFonts.nunitoSans(
                                        fontSize: 12,
                                        color: isDone
                                            ? BatikColors.marun
                                            : BatikColors.sogaGelap,
                                      ),
                                      overflow: TextOverflow.ellipsis,
                                    ),
                                  ),
                                  const SizedBox(width: 4),
                                  Text(
                                    '${(f.bytes.length / 1024).toStringAsFixed(0)} KB',
                                    style: GoogleFonts.nunitoSans(
                                      fontSize: 11,
                                      color: BatikColors.abuTeks,
                                    ),
                                  ),
                                  if (!isUploading) ...[
                                    const SizedBox(width: 6),
                                    GestureDetector(
                                      onTap: () => removeFile(i),
                                      child: const Icon(
                                        Icons.close_rounded,
                                        size: 14,
                                        color: BatikColors.abuTeks,
                                      ),
                                    ),
                                  ],
                                ],
                              ),
                            );
                          },
                        ),
                      ),
                    ],
                    const SizedBox(height: 16),
                    Row(
                      children: [
                        Expanded(
                          child: TextButton(
                            onPressed: isUploading ? null : () => Navigator.of(ctx).pop(),
                            style: TextButton.styleFrom(
                              padding: const EdgeInsets.symmetric(vertical: 12),
                              shape: RoundedRectangleBorder(
                                borderRadius: BorderRadius.circular(10),
                              ),
                            ),
                            child: Text(
                              'Batal',
                              style: GoogleFonts.nunitoSans(
                                fontSize: 13,
                                color: isUploading
                                    ? BatikColors.abuTeks
                                    : BatikColors.abuTeks,
                              ),
                            ),
                          ),
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          child: ElevatedButton(
                            onPressed: (hasFiles && !isUploading) ? doUpload : null,
                            style: ElevatedButton.styleFrom(
                              backgroundColor: (hasFiles && !isUploading)
                                  ? BatikColors.marun
                                  : BatikColors.krem,
                              disabledBackgroundColor: BatikColors.krem,
                              padding: const EdgeInsets.symmetric(vertical: 12),
                              shape: RoundedRectangleBorder(
                                borderRadius: BorderRadius.circular(10),
                              ),
                              elevation: 0,
                            ),
                            child: isUploading
                                ? const SizedBox(
                                    width: 16,
                                    height: 16,
                                    child: CircularProgressIndicator(
                                      strokeWidth: 2,
                                      color: Colors.white,
                                    ),
                                  )
                                : Text(
                                    hasFiles
                                        ? 'Unggah (${selectedFiles.length})'
                                        : 'Unggah',
                                    style: GoogleFonts.nunitoSans(
                                      fontSize: 13,
                                      fontWeight: FontWeight.w600,
                                      color: (hasFiles && !isUploading)
                                          ? Colors.white
                                          : BatikColors.abuTeks,
                                    ),
                                  ),
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          );
        },
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final isMobile = MediaQuery.of(context).size.width < 768;
    final hasPending = widget.pendingDetailId != null;

    return Container(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 20),
      decoration: const BoxDecoration(
        color: BatikColors.putih,
        border: Border(top: BorderSide(color: BatikColors.garis)),
      ),
      child: isMobile
          ? _MobileInputLayout(
              hasText: _hasText,
              sending: widget.sending,
              hasPending: hasPending,
              isListening: _isListening,
              speechEnabled: _speechEnabled,
              modelSwitching: _modelSwitching,
              ragModeSwitching: _ragModeSwitching,
              ragMode: _ragMode,
              activeModelId: _activeModelId,
              selectedImageBytes: _selectedImageBytes, // Pass Image Bytes
              controller: widget.controller,
              focusNode: widget.focusNode,
              onSend: _triggerSend,
              onStop: widget.onStop,
              onStartListening: _startListening,
              onStopListening: _stopListening,
              onShowModelDialog: _showModelDialog,
              onShowUploadDialog: _showUploadDialog,
              onShowRagModeDialog: _showRagModeDialog,
              onPickImage: _pickImage, // Pass Image Picker action
              onClearImage: () => setState(() {
                _selectedImageBytes = null;
                _selectedImageName = null;
              }),
            )
          : _DesktopInputLayout(
              hasText: _hasText,
              sending: widget.sending,
              hasPending: hasPending,
              isListening: _isListening,
              speechEnabled: _speechEnabled,
              modelSwitching: _modelSwitching,
              ragModeSwitching: _ragModeSwitching,
              ragMode: _ragMode,
              activeModelId: _activeModelId,
              selectedImageBytes: _selectedImageBytes, // Pass Image Bytes
              controller: widget.controller,
              focusNode: widget.focusNode,
              onSend: _triggerSend,
              onStop: widget.onStop,
              onStartListening: _startListening,
              onStopListening: _stopListening,
              onShowModelDialog: _showModelDialog,
              onShowUploadDialog: _showUploadDialog,
              onShowRagModeDialog: _showRagModeDialog,
              onPickImage: _pickImage, // Pass Image Picker action
              onClearImage: () => setState(() {
                _selectedImageBytes = null;
                _selectedImageName = null;
              }),
            ),
    );
  }
}

// ---------------------------------------------------------------------------
// Mobile Input Layout
// ---------------------------------------------------------------------------

class _MobileInputLayout extends StatelessWidget {
  const _MobileInputLayout({
    required this.hasText,
    required this.sending,
    required this.hasPending,
    required this.isListening,
    required this.speechEnabled,
    required this.modelSwitching,
    required this.ragModeSwitching,
    required this.ragMode,
    required this.activeModelId,
    this.selectedImageBytes,
    required this.controller,
    required this.focusNode,
    required this.onSend,
    required this.onStop,
    required this.onStartListening,
    required this.onStopListening,
    required this.onShowModelDialog,
    required this.onShowUploadDialog,
    required this.onShowRagModeDialog,
    required this.onPickImage,
    required this.onClearImage,
  });

  final bool hasText;
  final bool sending;
  final bool hasPending;
  final bool isListening;
  final bool speechEnabled;
  final bool modelSwitching;
  final bool ragModeSwitching;
  final String ragMode;
  final String activeModelId;
  final Uint8List? selectedImageBytes;
  final TextEditingController controller;
  final FocusNode focusNode;
  final VoidCallback onSend;
  final VoidCallback? onStop;
  final VoidCallback onStartListening;
  final VoidCallback onStopListening;
  final Future<void> Function(BuildContext) onShowModelDialog;
  final void Function(BuildContext) onShowUploadDialog;
  final void Function(BuildContext) onShowRagModeDialog;
  final VoidCallback onPickImage;
  final VoidCallback onClearImage;

  String _getModelShortName(String modelId) {
    if (modelId.isEmpty) return 'Loading...';
    if (modelId.contains('mistral')) return 'Mistral';
    if (modelId.contains('qwen')) return 'Qwen';
    if (modelId.contains('gpt-oss')) return 'GPT OSS';
    if (modelId.contains('llama')) return 'Llama';
    return modelId.split('/').last.split('-').first;
  }

  String _getRagModeShortName(String mode) {
    return mode == 'improved' ? 'Improved' : 'Regular';
  }

  @override
  Widget build(BuildContext context) {
    if (activeModelId.isEmpty) {
    return const SizedBox(
      height: 56,
      child: Center(
        child: SizedBox(
          width: 20,
          height: 20,
          child: CircularProgressIndicator(
            strokeWidth: 2,
            color: BatikColors.marun,
          ),
        ),
      ),
    );
  }

    final isSendDisabled = sending || (!hasText && selectedImageBytes == null) || modelSwitching;

    return Column(
      children: [
        if (selectedImageBytes != null) ...[
          Container(
            alignment: Alignment.centerLeft,
            margin: const EdgeInsets.only(bottom: 12),
            child: Container(
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: BatikColors.garis),
              ),
              child: Stack(
                children: [
                  ClipRRect(
                    borderRadius: BorderRadius.circular(12),
                    child: Image.memory(
                      selectedImageBytes!,
                      height: 100,
                      width: 100,
                      fit: BoxFit.cover,
                    ),
                  ),
                  Positioned(
                    top: 4,
                    right: 4,
                    child: GestureDetector(
                      onTap: onClearImage,
                      child: Container(
                        padding: const EdgeInsets.all(4),
                        decoration: const BoxDecoration(
                          color: BatikColors.sogaGelap,
                          shape: BoxShape.circle,
                        ),
                        child: const Icon(Icons.close, size: 14, color: Colors.white),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
        Row(
          children: [
            Expanded(
              child: Tooltip(
                message: ragMode == 'improved' 
                    ? 'Mode: Improved' 
                    : 'Mode: Regular',
                child: ElevatedButton(
                  onPressed: ragModeSwitching ? null : () => onShowRagModeDialog(context),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: BatikColors.garis,
                    disabledBackgroundColor: BatikColors.krem,
                    side: const BorderSide(color: BatikColors.garis),
                    padding: const EdgeInsets.symmetric(vertical: 10),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12),
                    ),
                    elevation: 0,
                  ),
                  child: ragModeSwitching
                      ? const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: BatikColors.sogaMuda,
                          ),
                        )
                      : Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Icon(
                              ragMode == 'improved' 
                                  ? Icons.auto_awesome_rounded 
                                  : Icons.speed_rounded,
                              size: 15,
                              color: ragMode == 'improved' 
                                  ? BatikColors.marun 
                                  : BatikColors.sogaMuda,
                            ),
                            const SizedBox(width: 5),
                            Flexible(
                              child: Text(
                                _getRagModeShortName(ragMode),
                                style: GoogleFonts.nunitoSans(
                                  fontSize: 11,
                                  fontWeight: FontWeight.w500,
                                  color: ragMode == 'improved' 
                                      ? BatikColors.marun 
                                      : BatikColors.sogaMuda,
                                ),
                                overflow: TextOverflow.ellipsis,
                              ),
                            ),
                          ],
                        ),
                ),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Tooltip(
                message: 'Model: $activeModelId',
                child: ElevatedButton(
                  onPressed: modelSwitching ? null : () => onShowModelDialog(context),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: BatikColors.garis,
                    disabledBackgroundColor: BatikColors.krem,
                    side: const BorderSide(color: BatikColors.garis),
                    padding: const EdgeInsets.symmetric(vertical: 10),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12),
                    ),
                    elevation: 0,
                  ),
                  child: modelSwitching
                      ? const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: BatikColors.marun,
                          ),
                        )
                      : Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            const Icon(
                              Icons.auto_awesome_rounded,
                              size: 15,
                              color: BatikColors.marun,
                            ),
                            const SizedBox(width: 5),
                            Flexible(
                              child: Text(
                                _getModelShortName(activeModelId),
                                style: GoogleFonts.nunitoSans(
                                  fontSize: 12,
                                  fontWeight: FontWeight.w500,
                                  color: BatikColors.marun,
                                ),
                                overflow: TextOverflow.ellipsis,
                              ),
                            ),
                          ],
                        ),
                ),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Tooltip(
                message: 'Pilih Gambar (Vision RAG)',
                child: ElevatedButton(
                  onPressed: sending ? null : onPickImage,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: BatikColors.garis,
                    disabledBackgroundColor: BatikColors.krem,
                    side: const BorderSide(color: BatikColors.garis),
                    padding: const EdgeInsets.symmetric(vertical: 10),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12),
                    ),
                    elevation: 0,
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(
                        Icons.image_outlined,
                        color: BatikColors.sogaMuda,
                        size: 18,
                      ),
                      const SizedBox(width: 5),
                      Text(
                        'Foto',
                        style: GoogleFonts.nunitoSans(
                          fontSize: 11,
                          color: BatikColors.sogaMuda,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),
        ConstrainedBox(
          constraints: const BoxConstraints(maxHeight: 75, minHeight: 70),
          child: TextField(
            controller: controller,
            focusNode: focusNode,
            maxLines: null,
            keyboardType: TextInputType.multiline,
            textInputAction: TextInputAction.send,
            enabled: !sending && !modelSwitching,
            style: GoogleFonts.nunitoSans(
              fontSize: 15,
              color: BatikColors.sogaGelap,
              height: 1.5,
            ),
            cursorColor: BatikColors.marun,
            decoration: InputDecoration(
              hintText: 'Ketik pertanyaan Anda...',
              hintStyle: GoogleFonts.nunitoSans(
                fontSize: 14,
                color: BatikColors.abuTeks,
              ),
              filled: true,
              fillColor: BatikColors.kertas,
              contentPadding: const EdgeInsets.symmetric(
                horizontal: 16,
                vertical: 16,
              ),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(24),
                borderSide: const BorderSide(color: BatikColors.garis),
              ),
              enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(24),
                borderSide: const BorderSide(color: BatikColors.garis),
              ),
              focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(24),
                borderSide: const BorderSide(
                  color: BatikColors.marun,
                  width: 1.5,
                ),
              ),
              disabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(24),
                borderSide: const BorderSide(color: BatikColors.garis),
              ),
            ),
            onSubmitted: (_) {
              if (!isSendDisabled) {
                onSend();
              }
            },
          ),
        ),
        const SizedBox(height: 10),
        Row(
          children: [
            Expanded(
              child: Tooltip(
                message: 'Unggah PDF pengetahuan bot',
                child: ElevatedButton(
                  onPressed: sending ? null : () => onShowUploadDialog(context),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: BatikColors.garis,
                    disabledBackgroundColor: BatikColors.krem,
                    side: const BorderSide(color: BatikColors.garis),
                    padding: const EdgeInsets.symmetric(vertical: 10),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12),
                    ),
                    elevation: 0,
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(
                        Icons.upload_file_rounded,
                        color: BatikColors.sogaMuda,
                        size: 18,
                      ),
                      const SizedBox(width: 5),
                      Text(
                        'Upload PDF',
                        style: GoogleFonts.nunitoSans(
                          fontSize: 11,
                          color: BatikColors.sogaMuda,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Tooltip(
                message: isListening ? 'Berhenti Merekam' : 'Input Suara',
                child: ElevatedButton(
                  onPressed: speechEnabled
                      ? (isListening ? onStopListening : onStartListening)
                      : null,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: isListening
                        ? BatikColors.marun
                        : BatikColors.garis,
                        disabledBackgroundColor: BatikColors.krem,
                        side: const BorderSide(color: BatikColors.garis),
                    padding: const EdgeInsets.symmetric(vertical: 10),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12),
                    ),
                    elevation: 0,
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(
                        isListening ? Icons.mic_off_rounded : Icons.mic_none_rounded,
                        color: isListening ? Colors.white : BatikColors.sogaMuda,
                        size: 18,
                      ),
                      const SizedBox(width: 5),
                      Text(
                        isListening ? 'Stop' : 'Suara',
                        style: GoogleFonts.nunitoSans(
                          fontSize: 11,
                          color: isListening ? Colors.white : BatikColors.sogaMuda,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: hasPending
                  ? Tooltip(
                      message: 'Hentikan generate',
                      child: ElevatedButton(
                        onPressed: onStop,
                        style: ElevatedButton.styleFrom(
                          backgroundColor: BatikColors.marunGelap,
                          padding: const EdgeInsets.symmetric(vertical: 10),
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(12),
                          ),
                          elevation: 0,
                        ),
                        child: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            const Icon(
                              Icons.stop_rounded,
                              color: Colors.white,
                              size: 18,
                            ),
                            const SizedBox(width: 5),
                            Text(
                              'Stop',
                              style: GoogleFonts.nunitoSans(
                                fontSize: 11,
                                color: Colors.white,
                                fontWeight: FontWeight.w500,
                              ),
                            ),
                          ],
                        ),
                      ),
                    )
                  : ElevatedButton(
                      onPressed: isSendDisabled ? null : onSend,
                      style: ElevatedButton.styleFrom(
                        backgroundColor: (hasText || selectedImageBytes != null) && !sending && !modelSwitching
                            ? BatikColors.marun
                            : BatikColors.krem,
                        disabledBackgroundColor: BatikColors.krem,
                        padding: const EdgeInsets.symmetric(vertical: 10),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(12),
                        ),
                        elevation: 0,
                      ),
                      child: (sending || modelSwitching)
                          ? const SizedBox(
                              width: 16,
                              height: 16,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                color: BatikColors.marun,
                              ),
                            )
                          : Row(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                Icon(
                                  Icons.arrow_upward_rounded,
                                  color: isSendDisabled ? BatikColors.abuTeks : Colors.white,
                                  size: 18,
                                ),
                                const SizedBox(width: 5),
                                Text(
                                  'Kirim',
                                  style: GoogleFonts.nunitoSans(
                                    fontSize: 11,
                                    fontWeight: FontWeight.w600,
                                    color: isSendDisabled ? BatikColors.abuTeks : Colors.white,
                                  ),
                                ),
                              ],
                            ),
                    ),
            ),
          ],
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Desktop Input Layout
// ---------------------------------------------------------------------------

class _DesktopInputLayout extends StatelessWidget {
   const _DesktopInputLayout({
    required this.hasText,
    required this.sending,
    required this.hasPending,
    required this.isListening,
    required this.speechEnabled,
    required this.modelSwitching,
    required this.ragModeSwitching,
    required this.ragMode,
    required this.activeModelId,
    this.selectedImageBytes,
    required this.controller,
    required this.focusNode,
    required this.onSend,
    required this.onStop,
    required this.onStartListening,
    required this.onStopListening,
    required this.onShowModelDialog,
    required this.onShowUploadDialog,
    required this.onShowRagModeDialog,
    required this.onPickImage,
    required this.onClearImage,
  });

  final bool hasText;
  final bool sending;
  final bool hasPending;
  final bool isListening;
  final bool speechEnabled;
  final bool modelSwitching;
  final bool ragModeSwitching;
  final String ragMode;
  final String activeModelId;
  final Uint8List? selectedImageBytes;
  final TextEditingController controller;
  final FocusNode focusNode;
  final VoidCallback onSend;
  final VoidCallback? onStop;
  final VoidCallback onStartListening;
  final VoidCallback onStopListening;
  final Future<void> Function(BuildContext) onShowModelDialog;
  final void Function(BuildContext) onShowUploadDialog;
  final void Function(BuildContext) onShowRagModeDialog;
  final VoidCallback onPickImage;
  final VoidCallback onClearImage;

  String _getRagModeShortName(String mode) {
    return mode == 'improved' ? 'Improved' : 'Regular';
  }

  String _getModelShortName(String modelId) {
    if (modelId.isEmpty) return 'Loading...';
    if (modelId.contains('mistral')) return 'Mistral';
    if (modelId.contains('qwen')) return 'Qwen';
    if (modelId.contains('gpt-oss')) return 'GPT OSS';
    if (modelId.contains('llama')) return 'Llama';
    return modelId.split('/').last.split('-').first;
  }

  @override
  Widget build(BuildContext context) {
    if (activeModelId.isEmpty) {
      return const SizedBox(
        height: 56,
        child: Center(
          child: SizedBox(
            width: 20,
            height: 20,
            child: CircularProgressIndicator(
              strokeWidth: 2,
              color: BatikColors.marun,
            ),
          ),
        ),
      );
    }

   final isSendDisabled = sending || (!hasText && selectedImageBytes == null) || modelSwitching;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (selectedImageBytes != null) ...[
          Container(
            margin: const EdgeInsets.only(bottom: 12),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(12),
              border: Border.all(color: BatikColors.garis),
            ),
            child: Stack(
              children: [
                ClipRRect(
                  borderRadius: BorderRadius.circular(12),
                  child: Image.memory(
                    selectedImageBytes!,
                    height: 100,
                    width: 100,
                    fit: BoxFit.cover,
                  ),
                ),
                Positioned(
                  top: 4,
                  right: 4,
                  child: GestureDetector(
                    onTap: onClearImage,
                    child: Container(
                      padding: const EdgeInsets.all(4),
                      decoration: const BoxDecoration(
                        color: BatikColors.sogaGelap,
                        shape: BoxShape.circle,
                      ),
                      child: const Icon(Icons.close, size: 14, color: Colors.white),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
        Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Padding(
              padding: const EdgeInsets.only(bottom: 0),
              child: SizedBox(
                width: 48,
                height: 48,
                child: Tooltip(
                  message: 'Unggah PDF pengetahuan bot',
                  child: ElevatedButton(
                    onPressed: () => onShowUploadDialog(context),
                    style: ElevatedButton.styleFrom(
                      backgroundColor: BatikColors.garis,
                      disabledBackgroundColor: BatikColors.krem,
                      side: const BorderSide(color: BatikColors.garis),
                      padding: EdgeInsets.zero,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
                      elevation: 0,
                    ),
                    child: const Icon(
                      Icons.upload_file_rounded,
                      color: BatikColors.sogaMuda,
                      size: 20,
                    ),
                  ),
                ),
              ),
            ),
            const SizedBox(width: 8),

            SizedBox(
              height: 48,
              child: Tooltip(
                message: ragMode == 'improved' 
                    ? 'Mode: Improved (Neo4j + Context)' 
                    : 'Mode: Regular (Simple)',
                child: ElevatedButton(
                  onPressed: ragModeSwitching ? null : () => onShowRagModeDialog(context),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: BatikColors.garis,
                    disabledBackgroundColor: BatikColors.krem,
                    side: const BorderSide(color: BatikColors.garis),
                    padding: const EdgeInsets.symmetric(horizontal: 10),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12),
                    ),
                    elevation: 0,
                  ),
                  child: ragModeSwitching
                      ? const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: BatikColors.sogaMuda,
                          ),
                        )
                      : Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            Icon(
                              ragMode == 'improved' 
                                  ? Icons.auto_awesome_rounded 
                                  : Icons.speed_rounded,
                              size: 15,
                              color: ragMode == 'improved' 
                                  ? BatikColors.marun 
                                  : BatikColors.sogaMuda,
                            ),
                            const SizedBox(width: 5),
                            Text(
                              _getRagModeShortName(ragMode),
                              style: GoogleFonts.nunitoSans(
                                fontSize: 11,
                                fontWeight: FontWeight.w500,
                                color: ragMode == 'improved' 
                                    ? BatikColors.marun 
                                    : BatikColors.sogaMuda,
                              ),
                            ),
                          ],
                        ),
                ),
              ),
            ),
            const SizedBox(width: 8),

            SizedBox(
              height: 48,
              child: Tooltip(
                message: 'Model: $activeModelId',
                child: ElevatedButton(
                  onPressed: modelSwitching ? null : () => onShowModelDialog(context),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: BatikColors.garis,
                    disabledBackgroundColor: BatikColors.krem,
                    side: const BorderSide(color: BatikColors.garis),
                    padding: const EdgeInsets.symmetric(horizontal: 10),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(12),
                    ),
                    elevation: 0,
                  ),
                  child: modelSwitching
                      ? const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: BatikColors.marun,
                          ),
                        )
                      : Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            const Icon(
                              Icons.auto_awesome_rounded,
                              size: 15,
                              color: BatikColors.marun,
                            ),
                            const SizedBox(width: 5),
                            Text(
                              _getModelShortName(activeModelId),
                              style: GoogleFonts.nunitoSans(
                                fontSize: 12,
                                fontWeight: FontWeight.w500,
                                color: BatikColors.marun,
                              ),
                            ),
                          ],
                        ),
                ),
              ),
            ),
            const SizedBox(width: 8),
            
            Padding(
              padding: const EdgeInsets.only(bottom: 0),
              child: SizedBox(
                width: 48,
                height: 48,
                child: Tooltip(
                  message: 'Pilih Gambar (Vision RAG)',
                  child: ElevatedButton(
                    onPressed: sending ? null : onPickImage,
                    style: ElevatedButton.styleFrom(
                      backgroundColor: BatikColors.garis,
                      disabledBackgroundColor: BatikColors.krem,
                      side: const BorderSide(color: BatikColors.garis),
                      padding: EdgeInsets.zero,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
                      elevation: 0,
                    ),
                    child: const Icon(
                      Icons.image_outlined,
                      color: BatikColors.sogaMuda,
                      size: 20,
                    ),
                  ),
                ),
              ),
            ),
            const SizedBox(width: 10),

            Expanded(
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxHeight: 150),
                child: Shortcuts(
                  shortcuts: <LogicalKeySet, Intent>{
                    LogicalKeySet(LogicalKeyboardKey.enter): const SendMessageIntent(),
                    LogicalKeySet(LogicalKeyboardKey.numpadEnter): const SendMessageIntent(),
                  },
                  child: Actions(
                    actions: <Type, Action<Intent>>{
                      SendMessageIntent: CallbackAction<SendMessageIntent>(
                        onInvoke: (intent) {
                          if (!isSendDisabled) {
                            onSend();
                          }
                          return null;
                        },
                      ),
                    },
                    child: TextField(
                      controller: controller,
                      focusNode: focusNode,
                      maxLines: null,
                      keyboardType: TextInputType.multiline,
                      textInputAction: TextInputAction.send,
                      enabled: !sending && !modelSwitching,
                      style: GoogleFonts.nunitoSans(
                        fontSize: 14,
                        color: BatikColors.sogaGelap,
                      ),
                      cursorColor: BatikColors.marun,
                      decoration: InputDecoration(
                        hintText: 'Ketik pertanyaan Anda...',
                        hintStyle: GoogleFonts.nunitoSans(
                          fontSize: 14,
                          color: BatikColors.abuTeks,
                        ),
                        filled: true,
                        fillColor: BatikColors.kertas,
                        contentPadding: const EdgeInsets.symmetric(
                          horizontal: 16,
                          vertical: 12,
                        ),
                        border: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(24),
                          borderSide: const BorderSide(color: BatikColors.garis),
                        ),
                        enabledBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(24),
                          borderSide: const BorderSide(color: BatikColors.garis),
                        ),
                        focusedBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(24),
                          borderSide: const BorderSide(
                            color: BatikColors.marun,
                            width: 1.5,
                          ),
                        ),
                        disabledBorder: OutlineInputBorder(
                          borderRadius: BorderRadius.circular(24),
                          borderSide: const BorderSide(color: BatikColors.garis),
                        ),
                      ),
                      onSubmitted: (_) {
                        if (!isSendDisabled) {
                          onSend();
                        }
                      },
                    ),
                  ),
                ),
              ),
            ),
            const SizedBox(width: 10),

            Padding(
              padding: const EdgeInsets.only(bottom: 0),
              child: SizedBox(
                width: 48,
                height: 48,
                child: Tooltip(
                  message: isListening ? 'Berhenti Merekam' : 'Input Suara',
                  child: ElevatedButton(
                    onPressed: speechEnabled
                        ? (isListening ? onStopListening : onStartListening)
                        : null,
                    style: ElevatedButton.styleFrom(
                      backgroundColor: isListening
                          ? BatikColors.marun
                          : BatikColors.garis,
                          disabledBackgroundColor: BatikColors.krem,
                          side: const BorderSide(color: BatikColors.garis),
                      padding: EdgeInsets.zero,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
                      elevation: 0,
                    ),
                    child: Icon(
                      isListening ? Icons.mic_off_rounded : Icons.mic_none_rounded,
                      color: isListening ? Colors.white : BatikColors.sogaMuda,
                      size: 20,
                    ),
                  ),
                ),
              ),
            ),
            const SizedBox(width: 10),

            SizedBox(
              width: 48,
              height: 48,
              child: hasPending
                  ? Tooltip(
                      message: 'Hentikan generate',
                      child: ElevatedButton(
                        onPressed: onStop,
                        style: ElevatedButton.styleFrom(
                          backgroundColor: BatikColors.marunGelap,
                          padding: EdgeInsets.zero,
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(12),
                          ),
                          elevation: 0,
                        ),
                        child: const Icon(
                          Icons.stop_rounded,
                          color: Colors.white,
                          size: 20,
                        ),
                      ),
                    )
                  : ElevatedButton(
                      onPressed: isSendDisabled ? null : onSend,
                      style: ElevatedButton.styleFrom(
                        backgroundColor: (hasText || selectedImageBytes != null) && !sending && !modelSwitching
                            ? BatikColors.marun
                            : BatikColors.krem,
                        disabledBackgroundColor: BatikColors.krem,
                        padding: EdgeInsets.zero,
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(12),
                        ),
                        elevation: 0,
                      ),
                      child: (sending || modelSwitching)
                          ? const SizedBox(
                              width: 18,
                              height: 18,
                              child: CircularProgressIndicator(
                                strokeWidth: 2,
                                color: BatikColors.marun,
                              ),
                            )
                          : Icon(
                              Icons.arrow_upward_rounded,
                              color: isSendDisabled ? BatikColors.abuTeks : Colors.white,
                              size: 20,
                            ),
                    ),
            ),
          ],
        ),
      ],
    );
  }
}